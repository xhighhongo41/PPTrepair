"""Tests for the synthetic-.pptx and corruption-injection fixture helpers."""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest
from fixtures import (
    build_minimal_pptx,
    build_zip_with_data_descriptors,
    find_eocd,
    foreign_prefix,
    truncate,
    version_mix,
    zero_prefix,
    zero_range,
)

#: Every part expected in a build_minimal_pptx() archive with 3 slides.
_EXPECTED_ENTRIES_3_SLIDES = {
    "[Content_Types].xml",
    "_rels/.rels",
    "ppt/presentation.xml",
    "ppt/_rels/presentation.xml.rels",
    "ppt/slides/slide1.xml",
    "ppt/slides/_rels/slide1.xml.rels",
    "ppt/slides/slide2.xml",
    "ppt/slides/_rels/slide2.xml.rels",
    "ppt/slides/slide3.xml",
    "ppt/slides/_rels/slide3.xml.rels",
    "ppt/slideMasters/slideMaster1.xml",
    "ppt/slideMasters/_rels/slideMaster1.xml.rels",
    "ppt/slideLayouts/slideLayout1.xml",
    "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
    "ppt/theme/theme1.xml",
    "ppt/media/image1.png",
    "ppt/presProps.xml",
    "ppt/viewProps.xml",
    "ppt/tableStyles.xml",
    "docProps/core.xml",
    "docProps/app.xml",
}


class TestBuildMinimalPptx:
    """Tests for build_minimal_pptx()."""

    def test_opens_as_valid_zip(self) -> None:
        """The archive must be openable and internally consistent."""
        data = build_minimal_pptx()
        with zipfile.ZipFile(BytesIO(data)) as zf:
            assert zf.testzip() is None

    def test_contains_expected_entries(self) -> None:
        """All expected package parts must be present for 3 slides."""
        data = build_minimal_pptx(num_slides=3)
        with zipfile.ZipFile(BytesIO(data)) as zf:
            assert set(zf.namelist()) == _EXPECTED_ENTRIES_3_SLIDES

    def test_size_at_least_media_bytes(self) -> None:
        """The overall archive size must track the requested media size."""
        media_bytes = 1_048_576
        data = build_minimal_pptx(media_bytes=media_bytes)
        assert len(data) >= media_bytes

    def test_deterministic_for_same_seed(self) -> None:
        """Two calls with the same arguments must produce identical bytes."""
        first = build_minimal_pptx(num_slides=2, media_bytes=10_000, seed=42)
        second = build_minimal_pptx(num_slides=2, media_bytes=10_000, seed=42)
        assert first == second


class TestZeroPrefix:
    """Tests for zero_prefix()."""

    def test_length_unchanged(self) -> None:
        """The overall length must not change."""
        data = b"A" * 100
        assert len(zero_prefix(data, 40)) == len(data)

    def test_prefix_is_zeroed_and_tail_preserved(self) -> None:
        """The first N bytes become zero; the remainder is untouched."""
        data = b"A" * 100
        result = zero_prefix(data, 40)
        assert result[:40] == b"\x00" * 40
        assert result[40:] == data[40:]


class TestZeroRange:
    """Tests for zero_range()."""

    def test_length_unchanged(self) -> None:
        """The overall length must not change."""
        data = b"B" * 100
        assert len(zero_range(data, 10, 30)) == len(data)

    def test_only_range_is_zeroed(self) -> None:
        """Only [start, end) becomes zero; the rest is untouched."""
        data = b"B" * 100
        result = zero_range(data, 10, 30)
        assert result[:10] == data[:10]
        assert result[10:30] == b"\x00" * 20
        assert result[30:] == data[30:]


class TestTruncate:
    """Tests for truncate()."""

    def test_result_has_requested_length(self) -> None:
        """The result must be exactly `length` bytes long."""
        data = b"C" * 100
        assert len(truncate(data, 37)) == 37

    def test_result_is_a_prefix(self) -> None:
        """The result must equal the original data's leading bytes."""
        data = bytes(range(256)) * 2
        assert truncate(data, 300) == data[:300]


class TestForeignPrefix:
    """Tests for foreign_prefix()."""

    def test_length_unchanged(self) -> None:
        """The overall length must not change."""
        data = build_minimal_pptx(num_slides=2, media_bytes=20_000, seed=5)
        result = foreign_prefix(data, 5000)
        assert len(result) == len(data)

    def test_no_pk_signature_in_replaced_region(self) -> None:
        """The replaced region must never contain a stray "PK" sequence."""
        data = build_minimal_pptx(num_slides=2, media_bytes=20_000, seed=5)
        length = 5000
        result = foreign_prefix(data, length)
        assert b"PK" not in result[:length]

    def test_replaced_region_is_not_all_zero(self) -> None:
        """The replacement must be non-zero (foreign) data, not zeros."""
        data = build_minimal_pptx(num_slides=2, media_bytes=20_000, seed=5)
        length = 5000
        result = foreign_prefix(data, length)
        assert result[:length] != b"\x00" * length

    def test_contains_marker(self) -> None:
        """The observed 4-byte marker must be embedded in the region."""
        data = build_minimal_pptx(num_slides=2, media_bytes=20_000, seed=5)
        length = 5000
        result = foreign_prefix(data, length)
        assert b"\x01\x00\x00\x00" in result[:length]

    def test_tail_preserved(self) -> None:
        """Bytes beyond the replaced region must be untouched."""
        data = build_minimal_pptx(num_slides=2, media_bytes=20_000, seed=5)
        length = 5000
        result = foreign_prefix(data, length)
        assert result[length:] == data[length:]


class TestFindEocd:
    """Tests for find_eocd()."""

    def test_finds_consistent_eocd(self) -> None:
        """cd_offset + cd_size must equal eocd_offset for an intact archive."""
        data = build_minimal_pptx(num_slides=2, media_bytes=10_000, seed=1)
        cd_offset, cd_size, eocd_offset = find_eocd(data)
        assert cd_offset + cd_size == eocd_offset

    def test_raises_when_signature_missing(self) -> None:
        """A ValueError must be raised when no EOCD signature exists."""
        with pytest.raises(ValueError):
            find_eocd(b"not a zip file at all")


class TestVersionMix:
    """Tests for version_mix()."""

    def _old_and_new(self) -> tuple[bytes, bytes]:
        """Build a small "old" archive and a larger, distinct "new" archive."""
        old = build_minimal_pptx(num_slides=2, media_bytes=200_000, seed=10)
        new = build_minimal_pptx(num_slides=5, media_bytes=1_500_000, seed=20)
        return old, new

    def test_length_matches_new(self) -> None:
        """The mixed archive's length must equal the new archive's length."""
        old, new = self._old_and_new()
        mixed = version_mix(old, new)
        assert len(mixed) == len(new)

    def test_head_matches_old(self) -> None:
        """The mixed archive's head must equal the old archive's head."""
        old, new = self._old_and_new()
        old_cd_offset, _cd_size, _eocd_offset = find_eocd(old)
        mixed = version_mix(old, new)
        assert mixed[:old_cd_offset] == old[:old_cd_offset]

    def test_still_opens_as_zip(self) -> None:
        """The surviving central directory from `new` must keep the archive openable."""
        old, new = self._old_and_new()
        mixed = version_mix(old, new)
        with zipfile.ZipFile(BytesIO(mixed)) as zf:
            assert zf.namelist()


class TestBuildZipWithDataDescriptors:
    """Tests for build_zip_with_data_descriptors()."""

    def test_opens_with_matching_contents(self) -> None:
        """The archive must open and yield back the original entry contents."""
        entries = {"a.txt": b"hello world" * 100, "b/c.txt": b"another entry" * 50}
        data = build_zip_with_data_descriptors(entries)
        with zipfile.ZipFile(BytesIO(data)) as zf:
            for name, content in entries.items():
                assert zf.read(name) == content

    def test_uses_data_descriptors(self) -> None:
        """At least one entry must have the data-descriptor flag bit set."""
        entries = {"a.txt": b"hello world" * 100, "b/c.txt": b"another entry" * 50}
        data = build_zip_with_data_descriptors(entries)
        with zipfile.ZipFile(BytesIO(data)) as zf:
            assert any(info.flag_bits & 0x08 for info in zf.infolist())
