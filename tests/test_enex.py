"""Tests for :mod:`pptrepair.enex`.

Every fixture here is a real Evernote export: a synthetic but
structurally faithful ``.enex`` document (DOCTYPE included, attachments
inlined as hard-wrapped base64, ``<data>`` written before the
``<file-name>`` that identifies it) written to ``tmp_path``. The
PowerPoint payloads inside come from :func:`fixtures.build_minimal_pptx`,
matching the approach used throughout this test suite.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fixtures import build_minimal_pptx, foreign_prefix, zero_prefix

from pptrepair import enex
from pptrepair.archive import ArchiveMember
from pptrepair.cancel import OperationCancelled
from pptrepair.enex import iter_materialized_attachments

#: The two PowerPoint presentation MIME types an export may declare.
_PPTX_MIME = ("application/vnd.openxmlformats-officedocument"
              ".presentationml.presentation")
_PPTM_MIME = "application/vnd.ms-powerpoint.presentation.macroEnabled.12"

#: Payload heads of the non-PowerPoint attachments used below; none of
#: them starts with a ZIP local file header.
_PDF_HEAD = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
_MP4_HEAD = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"


def _b64(data: bytes, width: int = 76) -> str:
    """Return *data* as base64 hard-wrapped every *width* characters."""
    text = base64.b64encode(data).decode("ascii")
    return "\n".join(text[i:i + width] for i in range(0, len(text), width))


def _resource(payload: bytes = b"", *, file_name: str | None = None,
              mime: str = "application/octet-stream",
              raw_data: str | None = None,
              encoding: str | None = "base64") -> str:
    """Build one ``<resource>`` element.

    *raw_data* overrides the base64 encoding of *payload* verbatim (used
    to inject damaged base64), and *file_name* left at None omits the
    whole ``<resource-attributes>`` block, which is how an export
    records an attachment that has no name of its own.
    """
    data = _b64(payload) if raw_data is None else raw_data
    attribute = "" if encoding is None else f' encoding="{encoding}"'
    attributes = ""
    if file_name is not None:
        attributes = ("<resource-attributes>"
                      f"<file-name>{file_name}</file-name>"
                      "</resource-attributes>")
    return (f"<resource><data{attribute}>{data}</data>"
            f"<mime>{mime}</mime>{attributes}</resource>")


def _note(title: str | None, *resources: str) -> str:
    """Build one ``<note>`` element with a CDATA body and *resources*."""
    head = "" if title is None else f"<title>{title}</title>"
    body = ("<content><![CDATA[<?xml version=\"1.0\"?>"
            "<en-note>body text &amp; markup</en-note>]]></content>")
    return f"<note>{head}{body}{''.join(resources)}</note>"


def _document(*notes: str) -> str:
    """Wrap *notes* in the export envelope, DOCTYPE included."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE en-export SYSTEM '
        '"http://xml.evernote.com/pub/evernote-export3.dtd">\n'
        '<en-export export-date="20240101T000000Z" '
        'application="Evernote" version="10.0">'
        f"{''.join(notes)}</en-export>"
    )


def _write_enex(path: Path, text: str) -> Path:
    """Write *text* to *path* as UTF-8 and return *path*."""
    path.write_bytes(text.encode("utf-8"))
    return path


def _dest(tmp_path: Path) -> Path:
    """Create and return the destination directory for one extraction."""
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    return dest_dir


def _collect(enex_path: Path, dest_dir: Path, **kwargs
             ) -> tuple[list[tuple[ArchiveMember, Path]], list[str]]:
    """Run the extractor to completion, returning its items and notes."""
    notes: list[str] = []
    items = list(iter_materialized_attachments(
        enex_path, dest_dir, on_note=notes.append, **kwargs))
    return items, notes


# --- selection: only PowerPoint attachments are extracted ------------------


def test_only_powerpoint_attachments_are_extracted(tmp_path: Path) -> None:
    """A PDF and a video attachment are dropped silently while the
    .pptx is extracted byte-for-byte under a "title/file-name" member."""
    pptx = build_minimal_pptx(num_slides=1, media_bytes=2048, seed=0)
    text = _document(
        _note("Deck note",
              _resource(pptx, file_name="deck.pptx", mime=_PPTX_MIME),
              _resource(_PDF_HEAD + b"x" * 4000, file_name="manual.pdf",
                        mime="application/pdf")),
        _note("Video note",
              _resource(_MP4_HEAD + b"y" * 4000, file_name="clip.mp4",
                        mime="video/mp4")),
    )
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert notes == []
    assert len(items) == 1
    member, path = items[0]
    assert member.archive_path == enex_path
    assert member.member_name == "Deck note/deck.pptx"
    assert member.size == len(pptx)
    assert path.parent == dest_dir
    assert path.read_bytes() == pptx


def test_several_notes_are_extracted_in_document_order(tmp_path: Path) -> None:
    """Attachments arrive in the order the export records them, each
    under its own note's title and a collision-free destination name."""
    first = build_minimal_pptx(num_slides=1, media_bytes=1024, seed=1)
    second = build_minimal_pptx(num_slides=2, media_bytes=1024, seed=2)
    text = _document(
        _note("Alpha", _resource(first, file_name="deck.pptx",
                                 mime=_PPTX_MIME)),
        _note("Beta", _resource(second, file_name="deck.pptx",
                                mime=_PPTX_MIME)),
    )
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert notes == []
    assert [m.member_name for m, _ in items] == ["Alpha/deck.pptx",
                                                 "Beta/deck.pptx"]
    assert [p.name for _, p in items] == ["member0000-deck.pptx",
                                          "member0001-deck.pptx"]
    assert [p.read_bytes() for _, p in items] == [first, second]


def test_untitled_note_uses_placeholder(tmp_path: Path) -> None:
    """A note without a <title> contributes "(untitled)" to member_name."""
    pptx = build_minimal_pptx(num_slides=1, media_bytes=512, seed=3)
    text = _document(_note(None, _resource(pptx, file_name="deck.pptm",
                                            mime=_PPTM_MIME)))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert notes == []
    assert [m.member_name for m, _ in items] == ["(untitled)/deck.pptm"]


# --- selection: fallbacks and exclusions -----------------------------------


@pytest.mark.parametrize(("mime", "expected"), [
    (_PPTX_MIME, "Slides/resource1.pptx"),
    (_PPTM_MIME, "Slides/resource1.pptm"),
])
def test_missing_file_name_falls_back_to_mime(tmp_path: Path, mime: str,
                                               expected: str) -> None:
    """Without a <file-name>, a PowerPoint <mime> still selects the
    resource, which is extracted under a synthesized name."""
    pptx = build_minimal_pptx(num_slides=1, media_bytes=512, seed=4)
    text = _document(_note("Slides", _resource(pptx, mime=mime)))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert notes == []
    assert [m.member_name for m, _ in items] == [expected]
    assert items[0][1].read_bytes() == pptx


def test_missing_file_name_with_other_mime_is_dropped(tmp_path: Path) -> None:
    """A nameless attachment whose MIME is not a presentation type is
    dropped silently, even though its payload happens to be a ZIP."""
    pptx = build_minimal_pptx(num_slides=1, media_bytes=512, seed=5)
    text = _document(_note("Slides", _resource(pptx, mime="application/zip")))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert (items, notes) == ([], [])
    assert list(dest_dir.iterdir()) == []


def test_office_lock_attachment_is_skipped_without_note(tmp_path: Path
                                                        ) -> None:
    """A ``~$`` owner/lock attachment is never extracted and never noted."""
    pptx = build_minimal_pptx(num_slides=1, media_bytes=512, seed=6)
    text = _document(_note("Locked",
                            _resource(pptx, file_name="~$deck.pptx",
                                      mime=_PPTX_MIME)))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert (items, notes) == ([], [])
    assert list(dest_dir.iterdir()) == []


def test_hostile_file_name_cannot_escape_dest_dir(tmp_path: Path) -> None:
    """A traversing <file-name> only influences the sanitized basename."""
    pptx = build_minimal_pptx(num_slides=1, media_bytes=512, seed=7)
    text = _document(_note("Evil",
                            _resource(pptx,
                                      file_name="../../escape.pptx",
                                      mime=_PPTX_MIME)))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert notes == []
    member, path = items[0]
    assert member.member_name == "Evil/../../escape.pptx"
    assert path == dest_dir / "member0000-escape.pptx"
    assert path.read_bytes() == pptx


# --- damaged resources -----------------------------------------------------


def test_invalid_base64_is_noted_and_other_resources_continue(
        tmp_path: Path) -> None:
    """A resource with damaged base64 is reported, while the healthy
    attachment after it -- and the one in the next note -- still arrive."""
    good = build_minimal_pptx(num_slides=1, media_bytes=512, seed=8)
    later = build_minimal_pptx(num_slides=2, media_bytes=512, seed=9)
    text = _document(
        _note("Damaged",
              _resource(raw_data="not valid base64!!!",
                        file_name="bad.pptx", mime=_PPTX_MIME),
              _resource(good, file_name="good.pptx", mime=_PPTX_MIME)),
        _note("Healthy",
              _resource(later, file_name="later.pptx", mime=_PPTX_MIME)),
    )
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert len(notes) == 1
    assert "cannot read attachment" in notes[0]
    assert "Damaged/bad.pptx" in notes[0]
    assert [m.member_name for m, _ in items] == ["Damaged/good.pptx",
                                                 "Healthy/later.pptx"]
    assert [p.read_bytes() for _, p in items] == [good, later]


def test_base64_cut_off_mid_quadruple_is_noted(tmp_path: Path) -> None:
    """A payload whose base64 length is not a multiple of four decodes
    cleanly up to the last whole quadruple and then fails at </data>,
    so the resource is reported rather than extracted half-written."""
    pptx = build_minimal_pptx(num_slides=1, media_bytes=512, seed=24)
    text = _document(_note("Cut",
                            _resource(raw_data=_b64(pptx)[:-2],
                                      file_name="deck.pptx",
                                      mime=_PPTX_MIME)))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert items == []
    assert len(notes) == 1
    assert "invalid base64 data" in notes[0]
    assert list(dest_dir.iterdir()) == []


def test_unsupported_data_encoding_is_noted(tmp_path: Path) -> None:
    """A <data> element declaring an encoding this module cannot decode
    is reported instead of being fed to the base64 decoder."""
    pptx = build_minimal_pptx(num_slides=1, media_bytes=512, seed=10)
    text = _document(_note("Odd",
                            _resource(pptx, file_name="deck.pptx",
                                      mime=_PPTX_MIME, encoding="hex")))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert items == []
    assert len(notes) == 1
    assert "unsupported data encoding" in notes[0]


# --- payload contents are never judged -------------------------------------


@pytest.mark.parametrize("damage", [zero_prefix, foreign_prefix])
def test_head_damaged_presentation_is_still_extracted(
        tmp_path: Path, damage: Callable[[bytes, int], bytes]) -> None:
    """A .pptx attachment whose head is zero-filled or overwritten with
    foreign data no longer starts with a ZIP signature -- and is still
    extracted byte-for-byte, because that is precisely the donor
    material the repair pipeline exists for."""
    broken = damage(build_minimal_pptx(num_slides=2, media_bytes=4096,
                                        seed=25), 2048)
    assert not broken.startswith(b"PK\x03\x04")
    text = _document(_note("Broken",
                            _resource(broken, file_name="broken.pptx",
                                      mime=_PPTX_MIME)))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert notes == []
    member, path = items[0]
    assert member.member_name == "Broken/broken.pptx"
    assert member.size == len(broken)
    assert path.read_bytes() == broken


def test_head_damaged_presentation_selected_by_mime_is_extracted(
        tmp_path: Path) -> None:
    """The same holds for the nameless, MIME-selected variant."""
    broken = zero_prefix(build_minimal_pptx(num_slides=1, media_bytes=1024,
                                             seed=26), 512)
    text = _document(_note("Broken", _resource(broken, mime=_PPTX_MIME)))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert notes == []
    assert [m.member_name for m, _ in items] == ["Broken/resource1.pptx"]
    assert items[0][1].read_bytes() == broken


def test_empty_payload_is_extracted_as_an_empty_file(tmp_path: Path) -> None:
    """An attachment with no payload at all is still extracted, as a
    zero-byte file; whether that is a usable presentation is not this
    module's call."""
    text = _document(_note("Hollow", _resource(b"", file_name="deck.pptx",
                                                mime=_PPTX_MIME)))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert notes == []
    member, path = items[0]
    assert member.member_name == "Hollow/deck.pptx"
    assert member.size == 0
    assert path.read_bytes() == b""


# --- spool hygiene ---------------------------------------------------------


def test_dest_dir_holds_only_the_extracted_files(tmp_path: Path) -> None:
    """Every discarded resource's spool file is removed again, so the
    destination directory ends up holding the extracted members only."""
    pptx = build_minimal_pptx(num_slides=1, media_bytes=2048, seed=11)
    other_zip = build_minimal_pptx(num_slides=1, media_bytes=2048, seed=12)
    text = _document(
        # Each of these is spooled in full -- the payload is never
        # examined -- and only ruled out at its own </resource>.
        _note("Mixed",
              _resource(_PDF_HEAD + b"z" * 8000, file_name="manual.pdf",
                        mime="application/pdf"),
              _resource(other_zip, file_name="bundle.zip",
                        mime="application/zip"),
              _resource(_MP4_HEAD + b"y" * 8000, mime="video/mp4"),
              _resource(pptx, file_name="deck.pptx", mime=_PPTX_MIME),
              _resource(pptx, file_name="~$deck.pptx", mime=_PPTX_MIME)),
    )
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert notes == []
    assert len(items) == 1
    assert sorted(p.name for p in dest_dir.iterdir()) == [
        "member0000-deck.pptx"]


def test_large_rejected_attachment_leaves_no_spool_behind(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-megabyte PDF attachment is spooled across many read
    chunks and then removed at its ``</resource>``, so the destination
    directory holds nothing but the .pptx that follows it."""
    monkeypatch.setattr(enex, "_FEED_CHUNK", 64 * 1024)
    pptx = build_minimal_pptx(num_slides=1, media_bytes=1024, seed=13)
    bulky = _PDF_HEAD + bytes(3 * 1024 * 1024)
    text = _document(
        _note("Bulky",
              _resource(bulky, file_name="huge.pdf", mime="application/pdf"),
              _resource(pptx, file_name="deck.pptx", mime=_PPTX_MIME)),
    )
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert notes == []
    assert [p.name for _, p in items] == ["member0000-deck.pptx"]
    assert sorted(p.name for p in dest_dir.iterdir()) == [
        "member0000-deck.pptx"]
    assert sum(p.stat().st_size for p in dest_dir.iterdir()) == len(pptx)


def test_spool_is_removed_when_a_run_is_cancelled_mid_attachment(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling while a large attachment is still being spooled leaves
    only the attachments already delivered behind, never a ``.part``."""
    monkeypatch.setattr(enex, "_FEED_CHUNK", 512)
    first = build_minimal_pptx(num_slides=1, media_bytes=512, seed=14)
    second = build_minimal_pptx(num_slides=1, media_bytes=200_000, seed=15)
    text = _document(
        _note("First", _resource(first, file_name="a.pptx", mime=_PPTX_MIME)),
        _note("Second", _resource(second, file_name="b.pptx",
                                  mime=_PPTX_MIME)),
    )
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)
    seen: list[str] = []

    def _cancel(fed: int, total: int) -> None:
        # Half the export is well inside the second (much larger)
        # attachment's base64, so its spool is open at this point --
        # asserted here so the cleanup below cannot pass vacuously.
        if fed > total // 2:
            assert list(dest_dir.glob("*.part"))
            raise OperationCancelled("stop")

    with pytest.raises(OperationCancelled):
        for member, _ in iter_materialized_attachments(enex_path, dest_dir,
                                                        progress=_cancel):
            seen.append(member.member_name)

    assert seen == ["First/a.pptx"]
    assert sorted(p.name for p in dest_dir.iterdir()) == ["member0000-a.pptx"]


def test_missing_dest_dir_degrades_to_notes(tmp_path: Path) -> None:
    """A destination directory that does not exist cannot be spooled to,
    which is reported per resource rather than raised."""
    pptx = build_minimal_pptx(num_slides=1, media_bytes=512, seed=22)
    text = _document(_note("Deck", _resource(pptx, file_name="deck.pptx",
                                              mime=_PPTX_MIME)))
    enex_path = _write_enex(tmp_path / "export.enex", text)

    items, notes = _collect(enex_path, tmp_path / "absent")

    assert items == []
    assert len(notes) == 1
    assert "cannot spool attachment data" in notes[0]


# --- damaged / unreadable exports ------------------------------------------


def test_truncated_export_yields_prefix_and_notes(tmp_path: Path) -> None:
    """An export cut off mid-document still delivers the attachments
    parsed before the cut, reports the failure and raises nothing."""
    first = build_minimal_pptx(num_slides=1, media_bytes=512, seed=16)
    second = build_minimal_pptx(num_slides=1, media_bytes=512, seed=17)
    text = _document(
        _note("First", _resource(first, file_name="a.pptx", mime=_PPTX_MIME)),
        _note("Second", _resource(second, file_name="b.pptx",
                                  mime=_PPTX_MIME)),
    )
    cut = text.index("</note>") + len("</note>") + 120
    enex_path = _write_enex(tmp_path / "export.enex", text[:cut])
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert [m.member_name for m, _ in items] == ["First/a.pptx"]
    assert items[0][1].read_bytes() == first
    assert len(notes) == 1
    assert notes[0].startswith(f"cannot parse enex {enex_path}: ")
    assert sorted(p.name for p in dest_dir.iterdir()) == ["member0000-a.pptx"]


def test_not_xml_at_all_is_noted(tmp_path: Path) -> None:
    """A file that is not XML degrades to a single note, no exception."""
    enex_path = _write_enex(tmp_path / "export.enex", "just plain text\n")
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert items == []
    assert len(notes) == 1
    assert notes[0].startswith(f"cannot parse enex {enex_path}: ")


def test_empty_export_is_noted(tmp_path: Path) -> None:
    """A zero-byte export is not a well-formed document either."""
    enex_path = _write_enex(tmp_path / "export.enex", "")
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert items == []
    assert notes == [f"cannot parse enex {enex_path}: file is empty"]


def test_external_entities_are_never_resolved(tmp_path: Path) -> None:
    """An export declaring an external entity never has it expanded, so
    a hostile DOCTYPE cannot exfiltrate a local file through a title."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET", encoding="utf-8")
    pptx = build_minimal_pptx(num_slides=1, media_bytes=512, seed=23)
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<!DOCTYPE en-export [<!ENTITY xxe SYSTEM "file://{secret}">]>\n'
        "<en-export>"
        + _note("&xxe;", _resource(pptx, file_name="deck.pptx",
                                    mime=_PPTX_MIME))
        + "</en-export>"
    )
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert all("TOP-SECRET" not in note for note in notes)
    assert all("TOP-SECRET" not in m.member_name for m, _ in items)


def test_missing_export_is_noted(tmp_path: Path) -> None:
    """An export that cannot even be opened degrades to a note."""
    enex_path = tmp_path / "missing.enex"
    dest_dir = _dest(tmp_path)

    items, notes = _collect(enex_path, dest_dir)

    assert items == []
    assert len(notes) == 1
    assert notes[0].startswith(f"cannot read enex {enex_path}: ")


# --- progress and cancellation ---------------------------------------------


def test_progress_is_monotonic_and_reaches_the_file_size(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """progress() is called per read chunk with a non-decreasing byte
    count whose final value is the export's size."""
    monkeypatch.setattr(enex, "_FEED_CHUNK", 64)
    pptx = build_minimal_pptx(num_slides=1, media_bytes=4096, seed=18)
    text = _document(_note("Deck", _resource(pptx, file_name="deck.pptx",
                                              mime=_PPTX_MIME)))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)
    calls: list[tuple[int, int]] = []

    items, notes = _collect(enex_path, dest_dir, progress=lambda fed, total:
                            calls.append((fed, total)))

    size = enex_path.stat().st_size
    assert notes == []
    assert len(calls) > 1
    assert all(total == size for _, total in calls)
    assert [fed for fed, _ in calls] == sorted(fed for fed, _ in calls)
    assert calls[-1][0] == size
    # The tiny feed chunks split the base64 at non-quadruple boundaries,
    # so this also proves the carried-over remainder is handled.
    assert items[0][1].read_bytes() == pptx


def test_progress_exception_propagates(tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch
                                       ) -> None:
    """Raising from progress() cancels the run: the exception is not
    swallowed, and no spool file survives."""
    monkeypatch.setattr(enex, "_FEED_CHUNK", 64)
    pptx = build_minimal_pptx(num_slides=1, media_bytes=4096, seed=19)
    text = _document(_note("Deck", _resource(pptx, file_name="deck.pptx",
                                              mime=_PPTX_MIME)))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    def _cancel(fed: int, total: int) -> None:
        raise OperationCancelled("stop")

    with pytest.raises(OperationCancelled):
        for _ in iter_materialized_attachments(enex_path, dest_dir,
                                               progress=_cancel):
            pass

    assert list(dest_dir.iterdir()) == []


def test_on_note_exception_propagates(tmp_path: Path) -> None:
    """A note callback may cancel the run just like progress()."""
    text = _document(_note("Broken",
                            _resource(raw_data="not valid base64!!!",
                                      file_name="broken.pptx",
                                      mime=_PPTX_MIME)))
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)

    def _cancel(message: str) -> None:
        raise OperationCancelled(message)

    with pytest.raises(OperationCancelled):
        for _ in iter_materialized_attachments(enex_path, dest_dir,
                                               on_note=_cancel):
            pass

    assert list(dest_dir.iterdir()) == []


# --- streaming behaviour ---------------------------------------------------


def test_attachments_arrive_before_the_export_is_fully_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first attachment is yielded while the tail of the export is
    still unread, which is what makes the generator streaming."""
    monkeypatch.setattr(enex, "_FEED_CHUNK", 512)
    first = build_minimal_pptx(num_slides=1, media_bytes=512, seed=20)
    second = build_minimal_pptx(num_slides=1, media_bytes=200_000, seed=21)
    text = _document(
        _note("First", _resource(first, file_name="a.pptx", mime=_PPTX_MIME)),
        _note("Second", _resource(second, file_name="b.pptx",
                                  mime=_PPTX_MIME)),
    )
    enex_path = _write_enex(tmp_path / "export.enex", text)
    dest_dir = _dest(tmp_path)
    seen: list[int] = []

    def _record(fed: int, total: int) -> None:
        seen.append(fed)

    iterator: Iterator[tuple[ArchiveMember, Path]] = (
        iter_materialized_attachments(enex_path, dest_dir, progress=_record))
    member, path = next(iterator)
    try:
        assert member.member_name == "First/a.pptx"
        assert path.read_bytes() == first
        assert seen[-1] < enex_path.stat().st_size
    finally:
        iterator.close()
