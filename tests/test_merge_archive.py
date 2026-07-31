"""Tests for archive SRC support in the ``pptrepair merge`` CLI subcommand.

Exercises :func:`pptrepair.cli.run_merge`'s handling of backup archives
(zip/tar) passed as SRC2+ -- expansion via :mod:`pptrepair.archive` into
plain on-disk members that feed the ordinary same-origin scoring and
:func:`pptrepair.merge.merge_restore` pipeline unchanged -- the same way
:mod:`test_merge_cli` covers the plain-file CLI path and
:mod:`test_archive` covers :mod:`pptrepair.archive` directly. The real
``broken_ppt/`` / ``normal_ppt/`` sample directories are never touched.
"""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from fixtures import (
    build_minimal_pptx,
    find_eocd,
    make_corrupted_copies,
    make_edited_version,
)

from pptrepair.cli import EXIT_ERROR, EXIT_OK, main

#: Shorthand for the capsys fixture type, to keep signatures short.
CaptureFixture = pytest.CaptureFixture[str]


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``tmp_path / name`` and return the resulting path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    """Write *entries* to a new zip archive at *path*."""
    with zipfile.ZipFile(path, mode="w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def _write_targz(path: Path, entries: dict[str, bytes]) -> None:
    """Write *entries* to a new gzip-compressed tar archive at *path*."""
    with tarfile.open(path, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _count_tar_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch tarfile.open to record the mode of every *read* open.

    Mirrors :func:`test_scan_archive._count_tar_reads`: returns the
    (initially empty) list the modes land in, ignoring write opens so a
    test may keep writing fixture archives after the patch is installed.
    """
    modes: list[str] = []
    real_open = tarfile.open

    def _counting_open(*args: object, **kwargs: object) -> tarfile.TarFile:
        mode = kwargs.get("mode", args[1] if len(args) > 1 else "r")
        if isinstance(mode, str) and mode.startswith("r"):
            modes.append(mode)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(tarfile, "open", _counting_open)
    return modes


def _entry_interval(data: bytes, name: str) -> tuple[int, int]:
    """Return the ``[offset, next_offset)`` byte range of member *name*.

    Mirrors :func:`test_merge._entry_interval` / :func:`test_merge_cli`'s
    own copy.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        offset = archive.getinfo(name).header_offset
        offsets = sorted(info.header_offset for info in archive.infolist())
    cd_offset, _size, _eocd = find_eocd(data)
    index = offsets.index(offset)
    end = offsets[index + 1] if index + 1 < len(offsets) else cd_offset
    return offset, end


def _lineage_versions() -> tuple[bytes, bytes]:
    """Return an original archive and a lineage version of it.

    Mirrors :func:`test_merge_cli._lineage_versions`: the version
    replaces slide1 with a longer body so the two differ in size while
    every media part stays byte-identical -- the shape
    :func:`pptrepair.origin.score_origin` recognises as a ``lineage``
    donor rather than a same-save copy. Mirrors
    :func:`test_merge_cli._lineage_versions` called with
    ``add_jpeg=False``, matching the truncated-copy material that test
    module's own ``test_degraded_lineage_donor_is_hybrid`` uses.
    """
    original = build_minimal_pptx(num_slides=3, media_bytes=60_000, seed=0)
    new_slide = (
        b"<p:sld><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/><p:sp>"
        b"<p:txBody><a:p><a:r><a:t>Edited slide body for the lineage "
        b"version, padded so the archive size clearly differs.</a:t>"
        b"</a:r></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )
    version = make_edited_version(
        original, replace={"ppt/slides/slide1.xml": new_slide})
    if len(version) == len(original):
        version = make_edited_version(
            original,
            replace={"ppt/slides/slide1.xml": new_slide + b"X" * 64})
    assert len(version) != len(original)
    return original, version


# --- archive-materialized twin: auto-tier splice -----------------------------


def test_zip_archive_twin_auto_tier_splices_full(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """An intact twin kept inside a ``.zip`` backup is scored ``auto`` and
    fully restores a corrupted target, with output byte-identical to the
    original."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    media_start, media_end = _entry_interval(data, "ppt/media/image1.png")
    (corrupted,) = make_corrupted_copies(
        data, [[("zero_range", media_start, media_end)]])
    path_a = _write(tmp_path, "a.pptx", corrupted)
    archive_path = tmp_path / "backup.zip"
    _write_zip(archive_path, {"backups/deck.pptx": data})
    out_path = tmp_path / "out.pptx"

    exit_code = main(
        ["merge", str(path_a), str(archive_path), "-o", str(out_path)])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert out_path.read_bytes() == data
    assert "Guarantee: full" in out_text


def test_targz_archive_twin_auto_tier_splices_full(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """The same recovery works from a ``.tar.gz`` backup, not only zip."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    media_start, media_end = _entry_interval(data, "ppt/media/image1.png")
    (corrupted,) = make_corrupted_copies(
        data, [[("zero_range", media_start, media_end)]])
    path_a = _write(tmp_path, "a.pptx", corrupted)
    archive_path = tmp_path / "backup.tar.gz"
    _write_targz(archive_path, {"backups/deck.pptx": data})
    out_path = tmp_path / "out.pptx"

    exit_code = main(
        ["merge", str(path_a), str(archive_path), "-o", str(out_path)])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert out_path.read_bytes() == data
    assert "Guarantee: full" in out_text


def test_targz_source_is_read_in_a_single_pass(
    tmp_path: Path, capsys: CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.tar.gz`` SRC is swept exactly once however many members it
    holds: enumerating and extracting are fused into one pass, so the
    work no longer grows with the member count (a compressed tar has to
    be decompressed from its first byte for every separate read)."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    media_start, media_end = _entry_interval(data, "ppt/media/image1.png")
    (corrupted,) = make_corrupted_copies(
        data, [[("zero_range", media_start, media_end)]])
    path_a = _write(tmp_path, "a.pptx", corrupted)
    archive_path = tmp_path / "backup.tar.gz"
    _write_targz(archive_path, {
        "backups/other1.pptx": build_minimal_pptx(num_slides=1,
                                                  media_bytes=20_000, seed=11),
        "backups/deck.pptx": data,
        "backups/other2.pptx": build_minimal_pptx(num_slides=1,
                                                  media_bytes=20_000, seed=12),
    })
    out_path = tmp_path / "out.pptx"
    modes = _count_tar_reads(monkeypatch)

    exit_code = main(
        ["merge", str(path_a), str(archive_path), "-o", str(out_path)])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert modes == ["r|*"]
    assert out_path.read_bytes() == data
    assert "Guarantee: full" in out_text


# --- archive-materialized lineage donor: hybrid, "::" display --------------


def test_lineage_donor_in_archive_used_with_yes_shows_archive_label(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A lineage-tier donor kept inside a zip backup is used with
    ``--yes``, the run reports ``hybrid``, and the donor is named by its
    ``"<archive>::<member>"`` label rather than any temporary path."""
    original, donor = _lineage_versions()
    media_start, media_end = _entry_interval(original, "ppt/media/image1.png")
    cut = (media_start + media_end) // 2
    copy_a, copy_b = make_corrupted_copies(original, [
        [("truncate", cut)],
        [("truncate", cut)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    archive_path = tmp_path / "backups.zip"
    _write_zip(archive_path, {"versions/donor.pptx": donor})

    exit_code = main(
        ["merge", str(path_a), str(path_b), str(archive_path),
         "-o", str(tmp_path / "out.pptx"), "--yes"])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "Guarantee: hybrid" in out_text
    assert f"{archive_path}::versions/donor.pptx" in out_text
    assert "member0000" not in out_text


# --- target cannot be an archive ---------------------------------------------


def test_target_inside_archive_is_usage_error(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """Passing an archive as SRC1 (the target) is a translated usage
    error, not an attempt to diagnose the archive itself."""
    data = build_minimal_pptx(num_slides=2, media_bytes=20_000)
    archive_path = tmp_path / "target.zip"
    _write_zip(archive_path, {"deck.pptx": data})
    path_b = _write(tmp_path, "b.pptx", data)

    exit_code = main(["merge", str(archive_path), str(path_b)])

    err = capsys.readouterr().err
    assert exit_code == EXIT_ERROR
    assert "pptrepair: error:" in err
    assert str(archive_path) in err


def test_target_archive_error_message_in_japanese(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """``--lang ja`` translates the target-is-an-archive error message."""
    data = build_minimal_pptx(num_slides=2, media_bytes=20_000)
    archive_path = tmp_path / "target.zip"
    _write_zip(archive_path, {"deck.pptx": data})
    path_b = _write(tmp_path, "b.pptx", data)

    exit_code = main(
        ["merge", str(archive_path), str(path_b), "--lang", "ja"])

    err = capsys.readouterr().err
    assert exit_code == EXIT_ERROR
    assert "マージ対象にアーカイブ内のファイルを指定することはできません" in err


# --- --json: origin_archive is set only for archive-derived sources --------


def test_json_origin_archive_field_marks_archive_sources_only(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A real-file source's ``origin_archive`` is null; an archive-derived
    source's is the archive's own path, and its ``path`` is the
    ``"<archive>::<member>"`` label rather than a temporary path."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    media_start, media_end = _entry_interval(data, "ppt/media/image1.png")
    slide_start, slide_end = _entry_interval(data, "ppt/slides/slide1.xml")
    copy_a, copy_b = make_corrupted_copies(data, [
        [("zero_range", media_start, media_end)],
        [("zero_range", slide_start, slide_end)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    archive_path = tmp_path / "backup.zip"
    _write_zip(archive_path, {"deck.pptx": data})
    out_path = tmp_path / "out.pptx"

    exit_code = main(
        ["merge", str(path_a), str(path_b), str(archive_path),
         "-o", str(out_path), "--json"])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    payload = json.loads(out_text)
    assert len(payload["sources"]) == 2
    by_path = {source["path"]: source for source in payload["sources"]}
    assert by_path[str(path_b)]["origin_archive"] is None
    archive_label = f"{archive_path}::deck.pptx"
    assert archive_label in by_path
    assert by_path[archive_label]["origin_archive"] == str(archive_path)


# --- zero-member archive: ignored with a note, run continues ----------------


def test_empty_archive_source_is_noted_and_ignored(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """An archive holding no ``.pptx``/``.pptm`` member is dropped with a
    note (naming the archive itself, never a temporary path); the merge
    still proceeds with the remaining real-file sources."""
    data = build_minimal_pptx(num_slides=2, media_bytes=20_000)
    path_a = _write(tmp_path, "a.pptx", data)
    path_b = _write(tmp_path, "b.pptx", data)
    empty_archive = tmp_path / "empty.zip"
    _write_zip(empty_archive, {"notes.txt": b"no pptx in here"})
    out_path = tmp_path / "out.pptx"

    exit_code = main(
        ["merge", str(path_a), str(path_b), str(empty_archive),
         "-o", str(out_path)])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert out_path.read_bytes() == data
    assert "has no usable" in out_text
    assert str(empty_archive) in out_text


def test_empty_targz_source_is_noted_and_ignored(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """The same degenerate note covers a tar-family backup, whose members
    are only ever discovered while the single extraction pass runs."""
    data = build_minimal_pptx(num_slides=2, media_bytes=20_000)
    path_a = _write(tmp_path, "a.pptx", data)
    path_b = _write(tmp_path, "b.pptx", data)
    empty_archive = tmp_path / "empty.tar.gz"
    _write_targz(empty_archive, {"notes.txt": b"no pptx in here"})
    out_path = tmp_path / "out.pptx"

    exit_code = main(
        ["merge", str(path_a), str(path_b), str(empty_archive),
         "-o", str(out_path)])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert out_path.read_bytes() == data
    assert "has no usable" in out_text
    assert str(empty_archive) in out_text


def test_unreadable_archive_keeps_its_own_note_instead_of_the_empty_one(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """An archive that cannot be read at all says so in its own words: the
    "no usable member" note is reserved for a readable archive that simply
    holds no presentation, and must not double up on this one."""
    data = build_minimal_pptx(num_slides=2, media_bytes=20_000)
    path_a = _write(tmp_path, "a.pptx", data)
    path_b = _write(tmp_path, "b.pptx", data)
    broken_archive = _write(tmp_path, "broken.zip", b"not a zip at all")
    out_path = tmp_path / "out.pptx"

    exit_code = main(
        ["merge", str(path_a), str(path_b), str(broken_archive),
         "-o", str(out_path)])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "cannot read archive" in out_text
    assert str(broken_archive) in out_text
    assert "has no usable" not in out_text


def test_empty_archive_only_other_source_falls_back_to_shortage_error(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """When the only other SRC is an archive with no usable member, the
    run falls back to the same usage error as too few raw SRC arguments."""
    data = build_minimal_pptx(num_slides=2, media_bytes=20_000)
    path_a = _write(tmp_path, "a.pptx", data)
    empty_archive = tmp_path / "empty.zip"
    _write_zip(empty_archive, {"notes.txt": b"no pptx in here"})

    exit_code = main(["merge", str(path_a), str(empty_archive)])

    err = capsys.readouterr().err
    assert exit_code == EXIT_ERROR
    assert "merge requires at least two SRC files" in err


# --- no temporary extraction path ever reaches user-facing output ----------


def test_no_temporary_extraction_path_leaks_into_output(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """Neither the text summary nor the ``--json`` output ever names the
    temporary directory or the ``memberNNNN-`` destination file an
    archive member was extracted to."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    media_start, media_end = _entry_interval(data, "ppt/media/image1.png")
    (corrupted,) = make_corrupted_copies(
        data, [[("zero_range", media_start, media_end)]])
    path_a = _write(tmp_path, "a.pptx", corrupted)
    archive_path = tmp_path / "backup.zip"
    _write_zip(archive_path, {"folder/deck.pptx": data})

    exit_code = main(
        ["merge", str(path_a), str(archive_path),
         "-o", str(tmp_path / "out1.pptx")])
    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "member0000" not in out_text
    assert "pptrepair-merge-" not in out_text
    assert f"{archive_path}::folder/deck.pptx" in out_text

    exit_code = main(
        ["merge", str(path_a), str(archive_path),
         "-o", str(tmp_path / "out2.pptx"), "--json"])
    out_json = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "member0000" not in out_json
    assert "pptrepair-merge-" not in out_json
    payload = json.loads(out_json)
    assert payload["sources"][0]["path"] == f"{archive_path}::folder/deck.pptx"
