"""Tests for the ``.enex`` dispatch added to :mod:`pptrepair.archive`.

An Evernote export is accepted as another donor container by the same
functions the pipeline already uses for zip/tar backups: :func:`is_archive`,
:func:`pptrepair.archive.iter_materialized_members`,
:func:`pptrepair.archive.list_members` and :func:`pptrepair.archive.materialize`
all recognise ``.enex`` and dispatch to :mod:`pptrepair.enex`. These tests
build a small, self-contained synthetic export (in the same spirit as
:mod:`tests.test_enex`'s fixtures) and check that dispatch, rather than
re-exercising every edge case :mod:`tests.test_enex` already covers.
"""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

import pytest
from fixtures import build_minimal_pptx

import pptrepair.archive as archive_module
from pptrepair.archive import (
    ArchiveMember,
    is_archive,
    iter_materialized_members,
    list_members,
    materialize,
)
from pptrepair.walker import discover_targets

#: The PowerPoint presentation MIME type used by the fixtures below.
_PPTX_MIME = ("application/vnd.openxmlformats-officedocument"
              ".presentationml.presentation")


def _b64(data: bytes, width: int = 76) -> str:
    """Return *data* as base64 hard-wrapped every *width* characters."""
    text = base64.b64encode(data).decode("ascii")
    return "\n".join(text[i:i + width] for i in range(0, len(text), width))


def _resource(payload: bytes, file_name: str,
              mime: str = _PPTX_MIME, encoding: str = "base64") -> str:
    """Build one ``<resource>`` element carrying *payload* as *file_name*."""
    return (f'<resource><data encoding="{encoding}">{_b64(payload)}</data>'
            f"<mime>{mime}</mime><resource-attributes>"
            f"<file-name>{file_name}</file-name>"
            "</resource-attributes></resource>")


def _note(title: str, *resources: str) -> str:
    """Build one ``<note>`` element with a CDATA body and *resources*."""
    body = ("<content><![CDATA[<?xml version=\"1.0\"?>"
            "<en-note>body text</en-note>]]></content>")
    return f"<note><title>{title}</title>{body}{''.join(resources)}</note>"


def _write_enex(path: Path, *notes: str) -> Path:
    """Write a minimal ``.enex`` export made of *notes* to *path*."""
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE en-export SYSTEM '
        '"http://xml.evernote.com/pub/evernote-export3.dtd">\n'
        '<en-export export-date="20240101T000000Z" '
        'application="Evernote" version="10.0">'
        f"{''.join(notes)}</en-export>"
    )
    path.write_bytes(text.encode("utf-8"))
    return path


# --- is_archive --------------------------------------------------------


def test_is_archive_accepts_enex_case_insensitively() -> None:
    """``.enex`` and ``.ENEX`` are both recognised as archive containers."""
    assert is_archive(Path("export.enex"))
    assert is_archive(Path("EXPORT.ENEX"))


# --- iter_materialized_members dispatch ---------------------------------


def test_iter_materialized_members_dispatches_to_enex(tmp_path: Path) -> None:
    """An ``.enex`` path is routed to :mod:`pptrepair.enex`, transparently
    passing *on_note* and *progress* through to the underlying export
    parser."""
    pptx = build_minimal_pptx(num_slides=1, media_bytes=512, seed=0)
    enex_path = _write_enex(
        tmp_path / "export.enex",
        _note("Deck note", _resource(pptx, "deck.pptx")),
        # A resource with an encoding the parser cannot decode: this is
        # what confirms on_note is actually wired through, not just
        # that extraction of the good resource happens to work.
        _note("Broken note",
              _resource(b"irrelevant", "bad.pptx",
                        encoding="quoted-printable")),
    )
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    notes: list[str] = []
    progress_calls: list[tuple[int, int]] = []
    items = list(iter_materialized_members(
        enex_path, dest_dir, on_note=notes.append,
        progress=lambda done, total: progress_calls.append((done, total))))

    assert len(items) == 1
    member, path = items[0]
    assert member.archive_path == enex_path
    assert member.member_name == "Deck note/deck.pptx"
    assert member.size == len(pptx)
    assert path.parent == dest_dir
    assert path.read_bytes() == pptx

    assert len(notes) == 1
    assert "bad.pptx" in notes[0]

    assert progress_calls
    last_done, last_total = progress_calls[-1]
    assert last_total > 0
    assert last_done == last_total


# --- list_members dispatch -----------------------------------------------


def test_list_members_dispatches_to_enex_and_leaves_no_leftovers(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``list_members`` reports the export's attachments and notes, and
    the temporary directory it spools attachments into during the full
    parse is removed again once the call returns."""
    pptx = build_minimal_pptx(num_slides=1, media_bytes=256, seed=1)
    enex_path = _write_enex(
        tmp_path / "export.enex",
        _note("Deck", _resource(pptx, "deck.pptx")),
    )

    captured_dirs: list[Path] = []
    real_temporary_directory = tempfile.TemporaryDirectory

    class _SpyTemporaryDirectory(real_temporary_directory):
        """Records the directory path handed out, for a leftover check."""

        def __enter__(self) -> str:
            name = super().__enter__()
            captured_dirs.append(Path(name))
            return name

    monkeypatch.setattr(archive_module.tempfile, "TemporaryDirectory",
                         _SpyTemporaryDirectory)
    members, notes = list_members(enex_path)

    assert notes == []
    assert len(members) == 1
    assert members[0].archive_path == enex_path
    assert members[0].member_name == "Deck/deck.pptx"
    assert members[0].size == len(pptx)

    assert captured_dirs, "list_members should have used a temporary directory"
    assert not captured_dirs[0].exists()


# --- materialize dispatch --------------------------------------------------


def test_materialize_dispatches_to_enex_keeping_only_requested_members(
        tmp_path: Path) -> None:
    """``materialize`` keeps only the requested members (deleting any other
    extracted attachment) and notes a requested member that was not found."""
    first = build_minimal_pptx(num_slides=1, media_bytes=256, seed=2)
    second = build_minimal_pptx(num_slides=2, media_bytes=256, seed=3)
    enex_path = _write_enex(
        tmp_path / "export.enex",
        _note("Alpha", _resource(first, "deck.pptx")),
        _note("Beta", _resource(second, "deck.pptx")),
    )

    members, notes = list_members(enex_path)
    assert notes == []
    assert len(members) == 2
    wanted = next(m for m in members if m.member_name == "Alpha/deck.pptx")
    missing = ArchiveMember(archive_path=enex_path,
                             member_name="Gamma/deck.pptx", size=123)

    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    extracted, notes = materialize(enex_path, [wanted, missing], dest_dir)

    assert set(extracted) == {wanted}
    assert extracted[wanted].read_bytes() == first
    # The unrequested "Beta/deck.pptx" attachment must not survive.
    assert list(dest_dir.iterdir()) == [extracted[wanted]]

    assert len(notes) == 1
    assert missing.display() in notes[0]
    assert "not found in export" in notes[0]


# --- walker integration (unchanged walker.py, sanity check) ---------------


def test_discover_targets_collects_enex_into_archives(tmp_path: Path) -> None:
    """With ``collect_archives=True`` an ``.enex`` file lands in the
    ``archives`` bucket, exactly like a zip/tar backup, without any change
    to :mod:`pptrepair.walker` itself."""
    enex_path = _write_enex(tmp_path / "export.enex", _note("Empty note"))

    result = discover_targets([tmp_path], collect_archives=True)

    assert result.archives == [enex_path]
    assert result.targets == []


# --- GUI source classification (skipped if PySide6 is unavailable) --------


def test_classify_source_enex_is_archive(tmp_path: Path) -> None:
    """The GUI's :func:`pptrepair.gui.sources.classify_source` classifies an
    ``.enex`` path as :data:`pptrepair.gui.sources.SourceKind.ARCHIVE`."""
    pytest.importorskip("PySide6")
    # Force the offscreen Qt platform plugin before the GUI module is
    # imported, matching tests/test_gui_sources.py -- classify_source
    # itself never creates a widget, but the module it lives in does
    # instantiate Qt classes at import time.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from pptrepair.gui.sources import SourceKind, classify_source

    enex_path = _write_enex(tmp_path / "export.enex", _note("Empty note"))

    assert classify_source(enex_path).kind is SourceKind.ARCHIVE
