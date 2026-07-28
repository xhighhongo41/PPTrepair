"""Tests for ``--search-archives`` in the ``scan`` / ``repair-all`` CLI.

Exercises :func:`pptrepair.cli.main` end to end on small synthetic trees
built under ``tmp_path`` (like :mod:`test_scan_cli`), each holding one or
more backup archives (zip/tar) whose ``.pptx`` members are mined as donor
material for the twin / lineage / merge candidate sections. Only the
opt-in ``--search-archives`` path is covered here; the archive-free
behaviour is pinned by :mod:`test_scan_cli` / :mod:`test_repair_all_cli`.
The real ``broken_ppt/`` / ``normal_ppt/`` sample directories are never
touched.
"""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest
from fixtures import (
    build_minimal_pptx,
    header_offset,
    make_corrupted_copies,
    make_edited_version,
    truncate,
    zero_entry_data_tail,
    zero_prefix,
)

from pptrepair import scan as scan_module
from pptrepair import walker as walker_module
from pptrepair.cancel import OperationCancelled
from pptrepair.cli import EXIT_CORRUPT, EXIT_OK, main

#: Shorthand for the capsys fixture type, to keep signatures short.
CaptureFixture = pytest.CaptureFixture[str]

#: Media payload large enough that a 256 KiB head zero-fill still leaves
#: surviving tail bytes (so the file classifies as head_zero_fill rather
#: than empty), yet small enough to keep fixtures fast.
_MEDIA_BYTES = 600_000

#: Head length zero-filled to synthesise a size-preserving corruption.
_ZERO_HEAD = 262_144

#: A slide-1 body distinctly larger than the fixture default, used to
#: build a genuinely different-sized lineage version of a synthetic
#: .pptx (mirrors :mod:`test_report`'s own ``_new_slide_xml``).
_NEW_SLIDE_XML = (
    b"<p:sld><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/><p:sp>"
    b"<p:txBody><a:p><a:r><a:t>Edited slide body for the lineage version, "
    b"padded so the archive size clearly differs.</a:t>"
    b"</a:r></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
)


def _write(dir_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``dir_path / name`` and return the resulting path."""
    path = dir_path / name
    path.write_bytes(data)
    return path


def _mkroot(tmp_path: Path, name: str = "root") -> Path:
    """Create and return an empty scan-root directory under *tmp_path*."""
    root = tmp_path / name
    root.mkdir()
    return root


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


def _file_entry(payload: dict, path: Path) -> dict:
    """Return the ``files`` entry whose ``path`` equals *path*."""
    return next(f for f in payload["files"] if f["path"] == str(path))


def _rebuildable_truncated(num_slides: int = 3) -> bytes:
    """Build a TAIL_TRUNCATED fixture that rebuilds with every slide intact."""
    data = build_minimal_pptx(num_slides=num_slides, media_bytes=4096)
    cutoff = header_offset(data, "ppt/media/image1.png")
    return truncate(data, cutoff)


# --- 1. archive twin candidate: "::" label, text + JSON ----------------------


def test_search_archives_surfaces_zip_twin_candidate(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A corrupted file's intact twin kept inside a zip backup is offered
    as a restore candidate, named by its ``"<archive>::<member>"`` label
    in both the text report and the JSON (with a non-null
    ``origin_archive``)."""
    original = build_minimal_pptx(media_bytes=_MEDIA_BYTES)
    broken_path = _write(_root := _mkroot(tmp_path), "broken.pptx",
                         zero_prefix(original, _ZERO_HEAD))
    archive_path = _root / "backup.zip"
    _write_zip(archive_path, {"backup/broken.pptx": original})
    report_dir = tmp_path / "report"

    exit_code = main(["scan", str(_root), "--report", str(report_dir),
                      "--json", "--search-archives"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_CORRUPT
    assert payload["schema_version"] == 4

    label = f"{archive_path}::backup/broken.pptx"
    entry = _file_entry(payload, broken_path)
    twin = next(t for t in entry["twin_candidates"] if t["path"] == label)
    assert twin["confidence"] == "high"
    assert twin["size"] == len(original)
    assert twin["origin_archive"] == str(archive_path)

    text = (report_dir / "scan_report.txt").read_text(encoding="utf-8")
    assert f"restore candidate: {label}" in text


def test_search_archives_surfaces_targz_twin_candidate(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """The same twin recovery works from a ``.tar.gz`` backup, not only zip."""
    original = build_minimal_pptx(media_bytes=_MEDIA_BYTES)
    broken_path = _write(_root := _mkroot(tmp_path), "broken.pptx",
                         zero_prefix(original, _ZERO_HEAD))
    archive_path = _root / "backup.tar.gz"
    _write_targz(archive_path, {"backup/broken.pptx": original})

    exit_code = main(["scan", str(_root), "--json", "--search-archives"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_CORRUPT
    label = f"{archive_path}::backup/broken.pptx"
    entry = _file_entry(payload, broken_path)
    assert any(t["path"] == label and t["origin_archive"] == str(archive_path)
               for t in entry["twin_candidates"])


# --- 2. archive lineage candidate --------------------------------------------


def test_search_archives_surfaces_lineage_candidate(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A corrupted file's different-sized version kept inside a backup is
    offered as a lineage candidate, named by its ``"::"`` label with the
    archive path in ``origin_archive``."""
    original = build_minimal_pptx(num_slides=3, media_bytes=60_000)
    version = make_edited_version(
        original, replace={"ppt/slides/slide1.xml": _NEW_SLIDE_XML})
    assert len(version) != len(original)
    (corrupted,) = make_corrupted_copies(
        original, [[("foreign_prefix", 4096)]])
    broken_path = _write(_root := _mkroot(tmp_path), "broken.pptx", corrupted)
    archive_path = _root / "versions.zip"
    _write_zip(archive_path, {"versions/v1.pptx": version})
    report_dir = tmp_path / "report"

    exit_code = main(["scan", str(_root), "--report", str(report_dir),
                      "--json", "--search-archives"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_CORRUPT
    label = f"{archive_path}::versions/v1.pptx"
    entry = _file_entry(payload, broken_path)
    lineage = next(c for c in entry["lineage_candidates"]
                   if c["path"] == label)
    assert lineage["origin_archive"] == str(archive_path)

    text = (report_dir / "scan_report.txt").read_text(encoding="utf-8")
    assert f"lineage candidate: {label}" in text


# --- 3. archive material in a merge-candidate group --------------------------


def test_search_archives_material_joins_merge_group(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A corrupted archive member sharing an exact byte size with a
    corrupted on-disk file joins its merge-candidate group, displayed by
    its ``"::"`` label."""
    base = build_minimal_pptx(num_slides=3, media_bytes=100_000)
    copy_a, copy_b = make_corrupted_copies(base, [
        [("foreign_prefix", 4096)],
        [("foreign_prefix", 8192)],
    ])
    disk_path = _write(_root := _mkroot(tmp_path), "a.pptx", copy_a)
    archive_path = _root / "backup.zip"
    _write_zip(archive_path, {"backup/b.pptx": copy_b})
    report_dir = tmp_path / "report"

    exit_code = main(["scan", str(_root), "--report", str(report_dir),
                      "--json", "--search-archives"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_CORRUPT
    label = f"{archive_path}::backup/b.pptx"
    group = next(g for g in payload["merge_groups"]
                 if g["size"] == len(base))
    assert str(disk_path) in group["files"]
    assert label in group["files"]

    text = (report_dir / "scan_report.txt").read_text(encoding="utf-8")
    merge_section = text.split("Merge candidates:")[1]
    assert label in merge_section


# --- 4. flag OFF: archives ignored, output unchanged -------------------------


def test_flag_off_ignores_archives_entirely(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """Without ``--search-archives`` an archive in the tree is ignored: no
    schema bump, no material keys, no archive-derived candidates, and the
    scanned count reflects only the on-disk .pptx files."""
    original = build_minimal_pptx(media_bytes=_MEDIA_BYTES)
    broken_path = _write(_root := _mkroot(tmp_path), "broken.pptx",
                         zero_prefix(original, _ZERO_HEAD))
    archive_path = _root / "backup.zip"
    _write_zip(archive_path, {"backup/broken.pptx": original})

    exit_code = main(["scan", str(_root), "--json"])

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert exit_code == EXIT_CORRUPT
    # The pre-archive schema carries no version / note fields at all.
    assert "schema_version" not in payload
    assert "archive_notes" not in payload
    # Only the on-disk file is scanned; the archive is neither counted
    # nor mined for donor material.
    assert payload["summary"]["scanned"] == 1
    assert "twin_candidates" not in _file_entry(payload, broken_path)
    assert str(archive_path) not in out


# --- 5. flag ON: schema 4, materials excluded from every tally ---------------


def test_material_never_counted_in_scanned_or_verdicts(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A mined archive member feeds the candidate sections but is absent
    from the scanned count and the verdict tally (schema_version 4)."""
    original = build_minimal_pptx(media_bytes=_MEDIA_BYTES)
    broken_path = _write(_root := _mkroot(tmp_path), "broken.pptx",
                         zero_prefix(original, _ZERO_HEAD))
    archive_path = _root / "backup.zip"
    _write_zip(archive_path, {"backup/broken.pptx": original})

    exit_code = main(["scan", str(_root), "--json", "--search-archives"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_CORRUPT
    assert payload["schema_version"] == 4
    # One on-disk target only; the intact member is not a scanned file.
    assert payload["summary"]["scanned"] == 1
    assert payload["summary"]["verdicts"] == {"head_zero_fill": 1}
    # Yet it is present as donor material (proving it was mined, not
    # counted).
    label = f"{archive_path}::backup/broken.pptx"
    entry = _file_entry(payload, broken_path)
    assert any(t["path"] == label for t in entry["twin_candidates"])


# --- 6. unreadable archive: noted, scan still completes ----------------------


def test_unreadable_archive_is_noted_and_scan_completes(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A ``.zip`` that cannot be opened yields an archive note without
    aborting the scan of the surrounding tree."""
    broken_path = _write(_root := _mkroot(tmp_path), "broken.pptx",
                         zero_prefix(build_minimal_pptx(
                             media_bytes=_MEDIA_BYTES), _ZERO_HEAD))
    bad_archive = _root / "corrupt.zip"
    bad_archive.write_bytes(b"this is not a valid zip archive at all")

    exit_code = main(["scan", str(_root), "--json", "--search-archives"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_CORRUPT
    # The scan of the on-disk tree still succeeded.
    assert payload["summary"]["scanned"] == 1
    assert _file_entry(payload, broken_path)["verdict"] == "head_zero_fill"
    # The unreadable archive is surfaced as a note, naming the archive.
    assert any("cannot read archive" in note and str(bad_archive) in note
               for note in payload["archive_notes"])


# --- 7. repair-all: candidates shown, material never repaired ----------------


def test_repair_all_never_repairs_archive_material(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """``repair-all --search-archives`` repairs only the on-disk corrupted
    files; the archive member feeds the candidate sections but produces no
    artifact and no repair entry of its own."""
    trunc = _rebuildable_truncated()
    original = build_minimal_pptx(num_slides=3, media_bytes=4096)
    trunc_path = _write(_root := _mkroot(tmp_path), "trunc.pptx", trunc)
    archive_path = _root / "backup.zip"
    _write_zip(archive_path, {"backup/trunc.pptx": original})
    out_dir = tmp_path / "out"

    exit_code = main(["repair-all", str(_root), "-o", str(out_dir),
                      "--json", "--search-archives"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_OK
    assert payload["schema_version"] == 4
    # Exactly one corrupted on-disk file was repaired; the material added
    # no repair entry.
    assert payload["counts"]["repaired"] == 1
    assert len(payload["repairs"]) == 1
    assert payload["repairs"][0]["path"] == str(trunc_path)
    # The member surfaces as a candidate in the embedded scan payload.
    label = f"{archive_path}::backup/trunc.pptx"
    scan_entry = next(f for f in payload["scan"]["files"]
                      if f["path"] == str(trunc_path))
    assert any(t["path"] == label for t in scan_entry.get("twin_candidates", []))
    # No artifact is ever derived from the archive member.
    repaired = list(out_dir.rglob("*.repaired.pptx"))
    assert [p.name for p in repaired] == ["trunc.repaired.pptx"]


# --- cloud-placeholder archives obey the download rule -----------------------


def test_placeholder_archive_skipped_without_allow_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture
) -> None:
    """A cloud-only placeholder archive is not mined without
    ``--allow-download``: it is counted as a skipped cloud-only file and
    offers no donor material (no implicit download)."""
    original = build_minimal_pptx(media_bytes=_MEDIA_BYTES)
    root = _mkroot(tmp_path)
    broken_path = _write(root, "broken.pptx", zero_prefix(original, _ZERO_HEAD))
    archive_path = root / "backup.zip"
    _write_zip(archive_path, {"backup/broken.pptx": original})
    arc_ino = archive_path.stat().st_ino
    monkeypatch.setattr(walker_module, "is_cloud_placeholder",
                        lambda st: st.st_ino == arc_ino)

    exit_code = main(["scan", str(root), "--json", "--search-archives"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_CORRUPT
    # The placeholder archive is left un-hydrated: no member is mined.
    assert "twin_candidates" not in _file_entry(payload, broken_path)
    # It is accounted for as a skipped cloud-only file instead.
    assert str(archive_path) in payload["skipped_cloud"]
    assert payload["summary"]["skipped"]["cloud_placeholder"] == 1


def test_placeholder_archive_mined_with_allow_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture
) -> None:
    """With ``--allow-download`` a placeholder archive is hydrated (its
    download announced on stderr first), mined, and its intact twin
    surfaces as a candidate; it is not also counted as skipped."""
    original = build_minimal_pptx(media_bytes=_MEDIA_BYTES)
    root = _mkroot(tmp_path)
    broken_path = _write(root, "broken.pptx", zero_prefix(original, _ZERO_HEAD))
    archive_path = root / "backup.zip"
    _write_zip(archive_path, {"backup/broken.pptx": original})
    arc_ino = archive_path.stat().st_ino
    monkeypatch.setattr(walker_module, "is_cloud_placeholder",
                        lambda st: st.st_ino == arc_ino)

    exit_code = main(["scan", str(root), "--json", "--search-archives",
                      "--allow-download"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == EXIT_CORRUPT
    # The impending hydration is announced before the archive is read.
    assert f"Downloading cloud-only file: {archive_path}" in captured.err
    # The archive is now mined and offers its intact twin as a candidate.
    label = f"{archive_path}::backup/broken.pptx"
    entry = _file_entry(payload, broken_path)
    assert any(t["path"] == label for t in entry["twin_candidates"])
    # It is not double-counted as a skipped cloud-only file.
    assert str(archive_path) not in payload["skipped_cloud"]


# --- 8. temporary extraction paths never leak --------------------------------


def test_no_temporary_extraction_path_leaks(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """Neither the JSON nor the written text/JSON reports ever name the
    temporary directory or the ``memberNNNN-`` destination file a member
    was extracted to; only the ``"::"`` label appears."""
    original = build_minimal_pptx(media_bytes=_MEDIA_BYTES)
    _write(_root := _mkroot(tmp_path), "broken.pptx",
           zero_prefix(original, _ZERO_HEAD))
    archive_path = _root / "backup.zip"
    _write_zip(archive_path, {"deep/folder/broken.pptx": original})
    report_dir = tmp_path / "report"

    exit_code = main(["scan", str(_root), "--report", str(report_dir),
                      "--json", "--search-archives"])

    out = capsys.readouterr().out
    report_txt = (report_dir / "scan_report.txt").read_text(encoding="utf-8")
    report_json = (report_dir / "scan_report.json").read_text(encoding="utf-8")
    assert exit_code == EXIT_CORRUPT
    for blob in (out, report_txt, report_json):
        assert "member0000" not in blob
        assert "pptrepair-scan-arc-" not in blob
    label = f"{archive_path}::deep/folder/broken.pptx"
    assert label in out
    assert label in report_txt


# --- 9. material_progress: per-member callback, coordinated cancellation -----


def test_material_progress_called_once_per_member(tmp_path: Path) -> None:
    """material_progress is invoked once per mined archive member, in the
    same order and with the same objects as ``ScanResult.materials``."""
    _write(_root := _mkroot(tmp_path), "normal.pptx",
          build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    archive_path = _root / "backup.zip"
    _write_zip(archive_path, {
        "backup/one.pptx": build_minimal_pptx(media_bytes=_MEDIA_BYTES, seed=1),
        "backup/two.pptx": build_minimal_pptx(media_bytes=_MEDIA_BYTES, seed=2),
    })

    calls = []
    result = scan_module.scan_paths([_root], search_archives=True,
                                    material_progress=calls.append)

    assert len(result.materials) == 2
    assert calls == result.materials


def test_material_progress_cancellation_propagates(tmp_path: Path) -> None:
    """A material_progress callback that raises OperationCancelled aborts
    archive mining right after the first member: the exception propagates
    uncaught, after exactly one call."""
    _write(_root := _mkroot(tmp_path), "normal.pptx",
          build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    archive_path = _root / "backup.zip"
    _write_zip(archive_path, {
        "backup/one.pptx": build_minimal_pptx(media_bytes=_MEDIA_BYTES, seed=1),
        "backup/two.pptx": build_minimal_pptx(media_bytes=_MEDIA_BYTES, seed=2),
    })

    calls = []

    def _cancel_on_first(material: scan_module.ArchiveMaterial) -> None:
        calls.append(material)
        raise OperationCancelled("user requested cancellation")

    with pytest.raises(OperationCancelled):
        scan_module.scan_paths([_root], search_archives=True,
                               material_progress=_cancel_on_first)

    assert len(calls) == 1


# --- 10. one-pass mining, session cache and archive progress -----------------


def _count_tar_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch tarfile.open to record the mode of every *read* open.

    Returns the (initially empty) list the modes land in. Write opens are
    ignored so a test may keep rewriting its fixture archives after the
    patch is installed.
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


def _two_member_targz(path: Path) -> None:
    """Write a two-member tar.gz backup of intact decks at *path*."""
    _write_targz(path, {
        "backup/one.pptx": build_minimal_pptx(num_slides=1, media_bytes=20_000,
                                              seed=1),
        "backup/two.pptx": build_minimal_pptx(num_slides=1, media_bytes=20_000,
                                              seed=2),
    })


def test_default_mining_keeps_the_materials_and_notes_contract(
    tmp_path: Path
) -> None:
    """Without a cache the (materials, notes) contract is unchanged: one
    material per extractable member, a damaged member noted and left out,
    and no extracted file surviving the call."""
    good = build_minimal_pptx(num_slides=1, media_bytes=20_000, seed=0)
    bad = build_minimal_pptx(num_slides=1, media_bytes=20_000, seed=1)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w",
                         compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup/good.pptx", good)
        zf.writestr("backup/bad.pptx", bad)
    archive_path = _mkroot(tmp_path) / "backup.zip"
    archive_path.write_bytes(zero_entry_data_tail(
        buffer.getvalue(), "backup/bad.pptx", keep_fraction=0.3))
    leftovers_before = set(
        Path(tempfile.gettempdir()).glob("pptrepair-scan-arc-*"))

    materials, notes = scan_module.diagnose_archive_materials([archive_path])

    assert [m.member.member_name for m in materials] == ["backup/good.pptx"]
    assert materials[0].error is None
    assert materials[0].diagnosis is not None
    assert materials[0].diagnosis.verdict.value == "normal"
    assert len(notes) == 1
    assert "backup/bad.pptx" in notes[0]
    # The temporary extraction directory never outlives the call.
    assert set(Path(tempfile.gettempdir()).glob(
        "pptrepair-scan-arc-*")) == leftovers_before


def test_cache_hit_serves_material_without_reopening_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second mining pass over an unchanged archive is served entirely
    from the session cache: the tar.gz is opened exactly once, the same
    materials and notes come back, and material_progress still fires once
    per member."""
    archive_path = _mkroot(tmp_path) / "backup.tar.gz"
    _two_member_targz(archive_path)
    cache = scan_module.ArchiveMaterialCache(tmp_path / "cache")
    reads = _count_tar_reads(monkeypatch)

    first, first_notes = scan_module.diagnose_archive_materials(
        [archive_path], cache=cache)
    replayed: list[scan_module.ArchiveMaterial] = []
    second, second_notes = scan_module.diagnose_archive_materials(
        [archive_path], cache=cache, material_progress=replayed.append)

    assert reads == ["r|*"]
    assert len(first) == 2
    assert second == first
    assert second_notes == first_notes == []
    assert replayed == second


def test_cache_entry_is_invalidated_when_the_archive_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive rewritten between two passes no longer matches its
    (size, mtime) stamp: it is mined again, its stale extraction directory
    is deleted, and the fresh material replaces the old."""
    archive_path = _mkroot(tmp_path) / "backup.tar.gz"
    _two_member_targz(archive_path)
    cache_root = tmp_path / "cache"
    cache = scan_module.ArchiveMaterialCache(cache_root)
    reads = _count_tar_reads(monkeypatch)

    first, _notes = scan_module.diagnose_archive_materials(
        [archive_path], cache=cache)
    _write_targz(archive_path, {
        "backup/three.pptx": build_minimal_pptx(num_slides=2,
                                                media_bytes=30_000, seed=3),
    })
    second, _notes = scan_module.diagnose_archive_materials(
        [archive_path], cache=cache)

    assert [m.member.member_name for m in first] == ["backup/one.pptx",
                                                     "backup/two.pptx"]
    assert [m.member.member_name for m in second] == ["backup/three.pptx"]
    assert reads == ["r|*", "r|*"]
    # The superseded arc0000 directory (and its files) is gone.
    assert sorted(p.name for p in cache_root.iterdir()) == ["arc0001"]


def test_cache_member_path_returns_the_extracted_donor(
    tmp_path: Path
) -> None:
    """member_path() hands the repair phase the donor's own bytes without
    re-reading the archive, and reports None for anything it cannot
    vouch for."""
    original = build_minimal_pptx(num_slides=1, media_bytes=20_000)
    archive_path = _mkroot(tmp_path) / "backup.zip"
    _write_zip(archive_path, {"backup/deck.pptx": original})
    cache = scan_module.ArchiveMaterialCache(tmp_path / "cache")

    materials, _notes = scan_module.diagnose_archive_materials(
        [archive_path], cache=cache)
    member = materials[0].member
    donor_path = cache.member_path(archive_path, member)

    assert donor_path is not None
    assert donor_path.read_bytes() == original
    # An archive that was never mined has nothing cached.
    assert cache.member_path(tmp_path / "other.zip", member) is None
    # Neither has one whose extracted file has since disappeared.
    donor_path.unlink()
    assert cache.member_path(archive_path, member) is None


def test_cached_material_survives_the_call_and_uncached_does_not(
    tmp_path: Path
) -> None:
    """With a cache the extracted members stay on disk under the cache
    root (that is what spares the repair phase a second full read); with
    no cache nothing is left behind at all."""
    archive_path = _mkroot(tmp_path) / "backup.tar.gz"
    _two_member_targz(archive_path)
    cache_root = tmp_path / "cache"
    cache = scan_module.ArchiveMaterialCache(cache_root)

    materials, _notes = scan_module.diagnose_archive_materials(
        [archive_path], cache=cache)

    kept = sorted(p.name for p in (cache_root / "arc0000").iterdir())
    assert kept == ["member0000-one.pptx", "member0001-two.pptx"]
    assert all(cache.member_path(archive_path, m.member) is not None
               for m in materials)

    leftovers_before = set(
        Path(tempfile.gettempdir()).glob("pptrepair-scan-arc-*"))
    scan_module.diagnose_archive_materials([archive_path])
    assert set(Path(tempfile.gettempdir()).glob(
        "pptrepair-scan-arc-*")) == leftovers_before


def test_cancelled_mining_is_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass cancelled part way through stores nothing and leaves no
    extraction directory, so the next pass mines the archive in full
    instead of serving a truncated view of it."""
    archive_path = _mkroot(tmp_path) / "backup.tar.gz"
    _two_member_targz(archive_path)
    cache_root = tmp_path / "cache"
    cache = scan_module.ArchiveMaterialCache(cache_root)
    reads = _count_tar_reads(monkeypatch)

    def _cancel_on_first(material: scan_module.ArchiveMaterial) -> None:
        raise OperationCancelled("user requested cancellation")

    with pytest.raises(OperationCancelled):
        scan_module.diagnose_archive_materials(
            [archive_path], cache=cache, material_progress=_cancel_on_first)

    assert cache.lookup(archive_path) is None
    assert list(cache_root.iterdir()) == []

    materials, _notes = scan_module.diagnose_archive_materials(
        [archive_path], cache=cache)
    assert [m.member.member_name for m in materials] == ["backup/one.pptx",
                                                         "backup/two.pptx"]
    assert reads == ["r|*", "r|*"]


def test_archive_progress_reports_bytes_tagged_with_the_archive(
    tmp_path: Path
) -> None:
    """archive_progress forwards the one-pass iterator's byte counters,
    tagged with the archive being read, and ends on that archive's size."""
    archive_path = _mkroot(tmp_path) / "backup.tar.gz"
    _two_member_targz(archive_path)

    calls: list[tuple[Path, int, int]] = []
    materials, notes = scan_module.diagnose_archive_materials(
        [archive_path],
        archive_progress=lambda path, done, total: calls.append(
            (path, done, total)))
    total_bytes = archive_path.stat().st_size

    assert len(materials) == 2
    assert notes == []
    assert calls
    assert {path for path, _done, _total in calls} == {archive_path}
    done_values = [done for _path, done, _total in calls]
    assert done_values == sorted(done_values)
    assert {total for _path, _done, total in calls} == {total_bytes}
    assert done_values[-1] == total_bytes


def test_scan_paths_forwards_the_cache_and_the_archive_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scan_paths hands its archive_cache/archive_progress straight to the
    mining pass: a second scan of the same tree reads the archive not at
    all, and the byte counters reach the caller tagged with the archive."""
    root = _mkroot(tmp_path)
    _write(root, "deck.pptx", build_minimal_pptx(num_slides=1,
                                                 media_bytes=20_000, seed=0))
    archive_path = root / "backup.tar.gz"
    _two_member_targz(archive_path)
    cache = scan_module.ArchiveMaterialCache(tmp_path / "cache")
    reads = _count_tar_reads(monkeypatch)
    calls: list[tuple[Path, int, int]] = []

    first = scan_module.scan_paths(
        [root], search_archives=True, archive_cache=cache,
        archive_progress=lambda path, done, total: calls.append(
            (path, done, total)))
    second = scan_module.scan_paths(
        [root], search_archives=True, archive_cache=cache)

    assert reads == ["r|*"]
    assert len(first.materials) == 2
    assert second.materials == first.materials
    assert calls
    assert {path for path, _done, _total in calls} == {archive_path}
    assert {total for _path, _done, total in calls} == {
        archive_path.stat().st_size}
