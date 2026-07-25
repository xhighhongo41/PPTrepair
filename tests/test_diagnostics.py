"""Tests for :mod:`pptrepair.diagnostics`.

Diagnosis objects are produced by the real scan -> census -> classify
pipeline (never hand-built dataclasses), so the fingerprint schema is
exercised against the same shapes the CLI would see in practice. All
fixtures are synthetic archives written under ``tmp_path``; no real
sample files are touched.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
import zlib
from pathlib import Path

from fixtures import (
    append_foreign_tail,
    build_foreign_zip,
    build_minimal_pptx,
    find_eocd,
    foreign_prefix,
    header_offset,
    lfh_offsets,
    overlay_foreign_zip_head,
    version_mix,
    zero_interior_entry,
    zero_prefix,
    zero_range,
)

from pptrepair import __version__
from pptrepair.census import from_central_directory, from_lfh_scan
from pptrepair.classify import Diagnosis, Verdict, classify
from pptrepair.diagnostics import (
    DIAG_SCHEMA_VERSION,
    HIGH_ENTROPY_THRESHOLD,
    build_fingerprint,
    chunk_profile,
    is_fingerprint_target,
)
from pptrepair.scanner import scan_structure

#: Matches a 12-hex-digit anonymous file identifier.
_FILE_ID_RE = re.compile(r"^[0-9a-f]{12}$")

#: Matches the ISO-8601 UTC, seconds-precision mtime format.
_MTIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``tmp_path / name`` and return the path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _diagnose(path: Path) -> Diagnosis:
    """Run the scan -> census -> classify pipeline over *path*."""
    structure = scan_structure(path)
    cd_census = from_central_directory(path)
    lfh_census = from_lfh_scan(path)
    return classify(path, structure, cd_census, lfh_census)


def _header_offset(data: bytes, name: str) -> int:
    """Return the local-file-header offset of *name* inside *data*."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.getinfo(name).header_offset


def _deterministic_random(seed: bytes, length: int) -> bytes:
    """Return *length* deterministic pseudo-random bytes, chained via sha256."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:length])


# --- is_fingerprint_target ---------------------------------------------------


def test_other_corrupt_is_a_fingerprint_target(tmp_path: Path) -> None:
    """(a) A file with unclassifiable, scattered damage (OTHER_CORRUPT) is
    a target.

    Combines a foreign-data head (destroying every entry before
    ``ppt/media/image1.png``) with a second, independent entry destroyed
    further into the archive (``ppt/viewProps.xml``, at ``skip=1`` past
    the media entry). Damage confined only to the head would classify as
    HEAD_FOREIGN_DATA; scattering it defeats that pattern's "confined to
    the head" guard, so the file stays unclassified.
    """
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    corrupted = foreign_prefix(data, 8192)
    scattered = zero_interior_entry(corrupted, skip=1)
    path = _write(tmp_path, "unknown.pptx", scattered)

    diag = _diagnose(path)

    assert diag.verdict == Verdict.OTHER_CORRUPT
    assert is_fingerprint_target(diag) is True


def test_plain_text_file_is_a_fingerprint_target(tmp_path: Path) -> None:
    """(b) A plain text file (NOT_A_ZIP, not CFB) is a target."""
    path = _write(tmp_path, "notes.pptx",
                  b"Hello, this is just a plain text file. " * 50)

    diag = _diagnose(path)

    assert diag.verdict == Verdict.NOT_A_ZIP
    assert is_fingerprint_target(diag) is True


def test_cfb_file_is_not_a_fingerprint_target(tmp_path: Path) -> None:
    """(c) An OLE compound document (NOT_A_ZIP, CFB head) is not a target."""
    cfb_signature = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    path = _write(tmp_path, "legacy.doc", cfb_signature + b"\xff" * 1000)

    diag = _diagnose(path)

    assert diag.verdict == Verdict.NOT_A_ZIP
    assert diag.structure is not None
    assert diag.structure.head_kind == "cfb"
    assert is_fingerprint_target(diag) is False


def test_normal_pptx_is_not_a_fingerprint_target(tmp_path: Path) -> None:
    """(d) An intact .pptx (NORMAL) is not a target."""
    data = build_minimal_pptx(num_slides=2)
    path = _write(tmp_path, "intact.pptx", data)

    diag = _diagnose(path)

    assert diag.verdict == Verdict.NORMAL
    assert is_fingerprint_target(diag) is False


def test_head_zero_fill_is_not_a_fingerprint_target(tmp_path: Path) -> None:
    """(e) A known pattern (HEAD_ZERO_FILL) is not a target."""
    data = build_minimal_pptx(num_slides=200, media_bytes=50_000)
    cutoff = _header_offset(data, "ppt/media/image1.png")
    path = _write(tmp_path, "zerofill.pptx", zero_prefix(data, cutoff))

    diag = _diagnose(path)

    assert diag.verdict == Verdict.HEAD_ZERO_FILL
    assert is_fingerprint_target(diag) is False


def test_empty_file_is_not_a_fingerprint_target(tmp_path: Path) -> None:
    """(f) A zero-byte file (EMPTY_FILE) is a known pattern, not a target."""
    path = _write(tmp_path, "empty.pptx", b"")

    diag = _diagnose(path)

    assert diag.verdict == Verdict.EMPTY_FILE
    assert is_fingerprint_target(diag) is False


def test_full_zero_fill_is_not_a_fingerprint_target(tmp_path: Path) -> None:
    """(g) An entirely zero-filled file (FULL_ZERO_FILL) is not a target."""
    path = _write(tmp_path, "zerofilled.pptx", b"\x00" * (256 * 1024))

    diag = _diagnose(path)

    assert diag.verdict == Verdict.FULL_ZERO_FILL
    assert is_fingerprint_target(diag) is False


def test_interior_damage_is_not_a_fingerprint_target(tmp_path: Path) -> None:
    """(h) A file with damage confined to interior entry data
    (INTERIOR_DAMAGE) is a known pattern, not a target."""
    data = build_minimal_pptx(num_slides=3, media_bytes=50_000)
    path = _write(tmp_path, "interior.pptx", zero_interior_entry(data))

    diag = _diagnose(path)

    assert diag.verdict == Verdict.INTERIOR_DAMAGE
    assert is_fingerprint_target(diag) is False


def test_tail_foreign_data_is_not_a_fingerprint_target(tmp_path: Path) -> None:
    """(i) A complete archive followed by foreign data (TAIL_FOREIGN_DATA)
    is a known pattern, not a target."""
    data = build_minimal_pptx(num_slides=1, media_bytes=50_000)
    path = _write(tmp_path, "tail.pptx", append_foreign_tail(data, 131072))

    diag = _diagnose(path)

    assert diag.verdict == Verdict.TAIL_FOREIGN_DATA
    assert is_fingerprint_target(diag) is False


#: Foreign, CRC-valid entries whose names the .pptx central directory
#: never lists (imitating a driver-package ZIP overwriting the file).
_FOREIGN_ENTRIES = {
    "DTT/drivers/x64/DptfPolicyCritical.dll": b"critical policy driver " * 40,
    "DTT/drivers/x64/DptfPolicyPassive.dll": b"passive policy driver " * 40,
    "DTT/drivers/x64/DptfPolicyLpm.dll": b"lpm policy driver blob " * 40,
}


def test_foreign_zip_overwrite_is_not_a_fingerprint_target(
    tmp_path: Path,
) -> None:
    """(j) A foreign-ZIP overwrite (FOREIGN_ZIP_OVERWRITE) is a known
    pattern, not a target."""
    data = build_minimal_pptx(num_slides=3, media_bytes=100_000)
    boundary = header_offset(data, "ppt/presProps.xml")
    corrupted = overlay_foreign_zip_head(
        data, boundary, build_foreign_zip(_FOREIGN_ENTRIES))
    tail = [off for off in lfh_offsets(corrupted) if off >= boundary]
    corrupted = zero_range(corrupted, tail[1], tail[2])
    path = _write(tmp_path, "foreign_zip.pptx", corrupted)

    diag = _diagnose(path)

    assert diag.verdict == Verdict.FOREIGN_ZIP_OVERWRITE
    assert is_fingerprint_target(diag) is False


def test_scattered_overwrite_is_not_a_fingerprint_target(
    tmp_path: Path,
) -> None:
    """(k) An in-place body overwrite (SCATTERED_OVERWRITE) is a known
    pattern, not a target."""
    data = build_minimal_pptx(num_slides=25, media_bytes=4096)
    cd_offset, _cd_size, _eocd_offset = find_eocd(data)
    path = _write(tmp_path, "scattered.pptx",
                  foreign_prefix(data, cd_offset, seed=5))

    diag = _diagnose(path)

    assert diag.verdict == Verdict.SCATTERED_OVERWRITE
    assert is_fingerprint_target(diag) is False


# --- chunk_profile -------------------------------------------------------------


def test_chunk_profile_run_length_merges_known_layout(tmp_path: Path) -> None:
    """Zero / text / high-entropy regions merge into three correct runs.

    The high-entropy region is built from deterministically generated
    pseudo-random bytes fed through :func:`zlib.compress`, which yields
    a compressed byte stream whose per-block entropy is high regardless
    of the (already high-entropy) input -- and includes a final, short
    block to exercise tail-block handling.
    """
    zero_region = b"\x00" * 2048  # 2 full 1024-byte blocks
    text_region = b"A" * 3072  # 3 full 1024-byte blocks, all printable
    raw = _deterministic_random(b"chunk-profile-seed", 8192)
    # 3 full blocks (3072 bytes) plus one short trailing block (528 bytes).
    entropy_region = zlib.compress(raw, level=9)[:3600]
    data = zero_region + text_region + entropy_region
    path = _write(tmp_path, "profile.bin", data)

    profile = chunk_profile(path, block_size=1024)

    assert len(profile) == 3
    assert profile[0] == {
        "offset": 0, "length": 2048, "class": "zeros", "mean_entropy": 0.0,
    }
    assert profile[1] == {
        "offset": 2048, "length": 3072, "class": "text_like",
        "mean_entropy": 0.0,
    }
    entropy_run = profile[2]
    assert entropy_run["offset"] == 5120
    assert entropy_run["length"] == 3600
    assert entropy_run["class"] == "high_entropy"
    assert entropy_run["mean_entropy"] >= HIGH_ENTROPY_THRESHOLD
    # mean_entropy is rounded to two decimals.
    scaled = entropy_run["mean_entropy"] * 100
    assert abs(scaled - round(scaled)) < 1e-9
    # The short trailing block was merged in, not dropped.
    assert sum(run["length"] for run in profile) == len(data)


def test_chunk_profile_empty_file_returns_empty_list(tmp_path: Path) -> None:
    """An empty file yields no runs at all."""
    path = _write(tmp_path, "empty.bin", b"")

    assert chunk_profile(path, block_size=1024) == []


# --- build_fingerprint schema ---------------------------------------------------


def _version_mix_diagnosis(tmp_path: Path) -> Diagnosis:
    """Build a VERSION_MIX diagnosis whose CD and LFH census both have
    entries, so the merged ``entries`` list exercises both source tags."""
    old = build_minimal_pptx(num_slides=2, media_bytes=200_000, seed=10)
    new = build_minimal_pptx(num_slides=5, media_bytes=1_500_000, seed=20)
    path = _write(tmp_path, "vm.pptx", version_mix(old, new))
    diag = _diagnose(path)
    assert diag.verdict == Verdict.VERSION_MIX
    return diag


def test_build_fingerprint_top_level_schema(tmp_path: Path) -> None:
    """Top-level keys, schema version, verdict and file metadata are correct."""
    diag = _version_mix_diagnosis(tmp_path)

    fp = build_fingerprint(diag)

    assert fp["kind"] == "pptrepair-diagnostic-fingerprint"
    assert fp["schema_version"] == DIAG_SCHEMA_VERSION == 1
    assert fp["tool_version"] == __version__
    assert fp["verdict"] == Verdict.VERSION_MIX.value
    assert fp["evidence"] == diag.evidence
    assert fp["salvage_summary"] == (diag.salvage_summary or None)
    assert fp["chunk_profile"] == chunk_profile(diag.path)

    file_info = fp["file"]
    assert _FILE_ID_RE.match(file_info["id"])
    assert file_info["name"] is None
    assert file_info["extension"] == ".pptx"
    assert file_info["size"] == diag.path.stat().st_size
    assert _MTIME_RE.fullmatch(file_info["mtime_utc"])


def test_build_fingerprint_extension_is_lowercased(tmp_path: Path) -> None:
    """A mixed-case extension is reported lowercased."""
    data = build_minimal_pptx(num_slides=1)
    path = _write(tmp_path, "Sample.PPTX", data)
    diag = _diagnose(path)

    fp = build_fingerprint(diag)

    assert fp["file"]["extension"] == ".pptx"


def test_build_fingerprint_zip_structure_zero_runs(tmp_path: Path) -> None:
    """``zip_structure.zero_runs`` reports exact alignment keys per run."""
    data = build_minimal_pptx(num_slides=200, media_bytes=50_000)
    cutoff = _header_offset(data, "ppt/media/image1.png")
    path = _write(tmp_path, "zerofill.pptx", zero_prefix(data, cutoff))
    diag = _diagnose(path)
    assert diag.verdict == Verdict.HEAD_ZERO_FILL
    assert diag.structure is not None
    assert diag.structure.zero_runs

    fp = build_fingerprint(diag)

    zip_structure = fp["zip_structure"]
    assert zip_structure is not None
    assert zip_structure["head_kind"] == diag.structure.head_kind
    assert zip_structure["size"] == diag.structure.size
    assert zip_structure["lfh_count"] == len(diag.structure.lfh_offsets)
    assert zip_structure["cd_sig_count"] == diag.structure.cd_sig_count
    assert len(zip_structure["zero_runs"]) == len(diag.structure.zero_runs)
    for reported, run in zip(zip_structure["zero_runs"], diag.structure.zero_runs):
        assert set(reported) == {"start", "end", "start_alignment", "end_alignment"}
        assert reported["start"] == run.start
        assert reported["end"] == run.end
        assert reported["start_alignment"] == run.start_alignment()
        assert reported["end_alignment"] == run.end_alignment()


def test_build_fingerprint_census_summaries(tmp_path: Path) -> None:
    """Each census summary's total/ok/errors_by_type/categories match the
    census objects produced by the real pipeline."""
    diag = _version_mix_diagnosis(tmp_path)

    fp = build_fingerprint(diag)

    for census, key in ((diag.cd_census, "cd"), (diag.lfh_census, "lfh")):
        assert census is not None
        summary = fp["census"][key]
        assert set(summary) == {"total", "ok", "errors_by_type", "categories"}
        assert summary["total"] == census.total()
        assert summary["ok"] == census.ok_count()

        expected_categories = {
            category: {"total": total, "ok": ok}
            for category, (ok, total) in census.category_stats().items()
        }
        assert summary["categories"] == expected_categories

        expected_errors: dict[str, int] = {}
        for entry in census.entries:
            if entry.ok:
                continue
            error_key = entry.error if entry.error is not None else "unknown"
            expected_errors[error_key] = expected_errors.get(error_key, 0) + 1
        assert summary["errors_by_type"] == expected_errors


def test_build_fingerprint_entries_source_tags(tmp_path: Path) -> None:
    """``entries`` lists CD-sourced entries first, then unlisted LFH ones."""
    diag = _version_mix_diagnosis(tmp_path)
    assert diag.cd_census is not None

    fp = build_fingerprint(diag)

    entries = fp["entries"]
    for entry in entries:
        assert set(entry) == {"name", "offset", "size", "ok", "error", "source"}

    cd_len = len(diag.cd_census.entries)
    assert all(e["source"] == "cd" for e in entries[:cd_len])
    assert all(e["source"] == "lfh" for e in entries[cd_len:])

    cd_offsets = {e.header_offset for e in diag.cd_census.entries}
    expected_lfh_extra = [
        e for e in diag.lfh_census.entries if e.header_offset not in cd_offsets
    ]
    assert len(entries) == cd_len + len(expected_lfh_extra)
    assert {"cd", "lfh"} <= {e["source"] for e in entries}


# --- privacy contract -----------------------------------------------------------


def test_fingerprint_excludes_document_content_and_path(tmp_path: Path) -> None:
    """No slide text, absolute path or parent directory name leaks out."""
    secret_dir = tmp_path / "TopSecretParentDir12345"
    secret_dir.mkdir()
    data = build_minimal_pptx(num_slides=2)
    path = _write(secret_dir, "MyConfidentialDeck.pptx", data)
    diag = _diagnose(path)
    assert diag.verdict == Verdict.NORMAL

    fp = build_fingerprint(diag)
    dumped = json.dumps(fp)

    # Slide body text (from fixtures._slide_xml) must never appear.
    assert "Slide 1 body text" not in dumped
    assert "Slide 2 body text" not in dumped
    assert "body text" not in dumped
    # Neither should the doc metadata written into docProps parts.
    assert "Fixture Author" not in dumped

    # No path information leaks by default.
    assert str(path.resolve()) not in dumped
    assert str(path.parent.resolve()) not in dumped
    assert "TopSecretParentDir12345" not in dumped
    assert "MyConfidentialDeck" not in dumped
    assert fp["file"]["name"] is None


def test_fingerprint_include_filename_reports_basename_only(tmp_path: Path) -> None:
    """With ``include_filename=True``, only the basename is reported."""
    secret_dir = tmp_path / "TopSecretParentDir12345"
    secret_dir.mkdir()
    data = build_minimal_pptx(num_slides=1)
    path = _write(secret_dir, "MyConfidentialDeck.pptx", data)
    diag = _diagnose(path)

    fp = build_fingerprint(diag, include_filename=True)
    dumped = json.dumps(fp)

    assert fp["file"]["name"] == "MyConfidentialDeck.pptx"
    # The basename is expected to appear now, but the parent directory
    # name and the full resolved path still must not.
    assert "TopSecretParentDir12345" not in dumped
    assert str(path.resolve()) not in dumped
