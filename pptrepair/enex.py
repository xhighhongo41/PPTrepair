"""Extraction of PowerPoint attachments from Evernote ``.enex`` exports.

An Evernote export is a single XML document that inlines every
attachment of every note as base64 text::

    <en-export>
      <note>
        <title>Quarterly review</title>
        <content><![CDATA[ ... note body ... ]]></content>
        <resource>
          <data encoding="base64">UEsDBBQ...</data>
          <mime>application/vnd.openxmlformats-...</mime>
          <resource-attributes>
            <file-name>deck.pptx</file-name>
          </resource-attributes>
        </resource>
      </note>
    </en-export>

Two properties of that layout dictate the whole design of this module.
First, real exports reach hundreds of megabytes and one attachment can
be a sizeable fraction of that, so neither a DOM nor
``ElementTree.iterparse`` is usable: both keep whole subtrees alive.
Second, ``<data>`` comes *before* the ``<file-name>``/``<mime>`` that
say whether the attachment is a presentation at all, so the payload has
to be spooled to disk before the keep/drop decision can be made.

:func:`iter_materialized_attachments` therefore drives a SAX parser
incrementally over fixed-size reads and decodes ``<data>`` as it
arrives, so the only things ever held in memory are one read chunk, at
most three carried-over base64 characters and a handful of short
metadata strings. Attachments are handed to the caller as
:class:`pptrepair.archive.ArchiveMember` values, exactly like members
mined out of a backup archive, so the diagnosis/repair pipeline treats
both sources identically.

Robustness follows the same rules as :mod:`pptrepair.archive`: a file
that cannot be opened or parsed degrades to a note instead of an
exception, one unreadable resource never stops the remaining ones, and
a resource's own ``<file-name>`` is never trusted to build a
destination path. Callback exceptions are the single deliberate
exception to "never raises": they propagate untouched, which is what
makes cooperative cancellation work (see
:class:`pptrepair.cancel.OperationCancelled`).
"""

from __future__ import annotations

import base64
import contextlib
import os
import posixpath
import tempfile
import xml.sax
import xml.sax.handler
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO
from xml.sax.xmlreader import AttributesImpl

from pptrepair.archive import ArchiveMember, _dest_filename
from pptrepair.walker import TARGET_SUFFIXES, TEMP_PREFIX

#: Number of raw bytes read from the export and handed to the SAX
#: parser per iteration. Large enough to keep the per-call overhead
#: negligible on a multi-hundred-megabyte export, small enough that the
#: resident set stays flat.
_FEED_CHUNK = 1 << 20

#: Name parts of the temporary spool files written while a resource's
#: payload is still being decoded. Always removed again, either by the
#: rename onto the final name or by the discard path.
_SPOOL_PREFIX = ".enex-spool-"
_SPOOL_SUFFIX = ".part"

#: Stand-in for the note title in ``member_name`` when a note carries
#: no ``<title>`` (or an empty one).
_UNTITLED = "(untitled)"

#: PowerPoint MIME types mapped to the suffix of the synthetic file
#: name used when a resource has no ``<file-name>`` of its own. Keys
#: are lower-cased, since the lookup is done on a lower-cased
#: ``<mime>`` (MIME types are case-insensitive).
_PRESENTATION_MIMES = {
    "application/vnd.openxmlformats-officedocument"
    ".presentationml.presentation": ".pptx",
    "application/vnd.ms-powerpoint.presentation.macroenabled.12": ".pptm",
}

#: Removes every whitespace character from a string; base64 payloads in
#: an export are hard-wrapped, and those line breaks are not part of
#: the encoded data.
_WHITESPACE = str.maketrans("", "", " \t\n\r\f\v")

#: One queued handler outcome: either a note (``str``) or a
#: materialized attachment (``(member, destination path)``). The
#: handler only ever appends to that queue; both the notes and the
#: attachments are delivered to the caller's callbacks from
#: :func:`_drain`, i.e. from outside any SAX callback, so a callback
#: exception can never be swallowed by the parser's own error handling.
_Pending = str | tuple[ArchiveMember, Path]


def _emit(on_note: Callable[[str], None] | None, message: str) -> None:
    """Hand *message* to *on_note*, when a callback was supplied.

    An exception raised by the callback is deliberately not caught; see
    the cancellation contract on
    :func:`iter_materialized_attachments`.
    """
    if on_note is not None:
        on_note(message)


class _AttachmentHandler(xml.sax.handler.ContentHandler):
    """SAX handler that spools ``.pptx``/``.pptm`` resources to disk.

    The handler is a pure producer: it appends every outcome -- a
    materialized attachment or a note about a resource it had to drop
    -- to :attr:`pending`, which :func:`iter_materialized_attachments`
    drains between parser feeds. Nothing here ever raises: an I/O or
    decoding failure is confined to the resource it happened in, so the
    parse always continues with the next one.
    """

    def __init__(self, enex_path: Path, dest_dir: Path) -> None:
        """Prepare a handler spooling *enex_path*'s attachments to *dest_dir*.

        *dest_dir* must already exist; creating and cleaning it up is
        the caller's responsibility (as in
        :func:`pptrepair.archive.materialize`).
        """
        super().__init__()
        self._enex_path = enex_path
        self._dest_dir = dest_dir
        self.pending: list[_Pending] = []

        # Document position. The element stack is only ever a handful of
        # entries deep and is used to qualify ambiguous tag names (e.g.
        # <data> is meaningful only directly inside a <resource>).
        self._stack: list[str] = []
        self._note_title: str | None = None
        self._text: list[str] | None = None
        self._in_data = False
        self._resource_open = False
        self._resource_ordinal = 0
        self._accepted = 0

        # Per-resource state, reset by _begin_resource().
        self._file_name: str | None = None
        self._mime: str | None = None
        self._error: str | None = None
        self._discard = False
        self._carry = ""
        self._decoded = 0
        self._spool: IO[bytes] | None = None
        self._spool_path: Path | None = None

    # -- SAX callbacks ----------------------------------------------------

    def startElement(self, name: str, attrs: AttributesImpl) -> None:
        """Enter *name*, arming whichever text/payload capture it starts."""
        parent = self._stack[-1] if self._stack else ""
        self._stack.append(name)
        if name == "note":
            self._note_title = None
            return
        if name == "resource" and parent == "note":
            self._begin_resource()
            return
        if self._resource_open:
            if parent == "resource":
                if name == "data":
                    self._begin_data(attrs.get("encoding"))
                elif name == "mime":
                    self._text = []
            elif name == "file-name" and parent == "resource-attributes":
                self._text = []
            return
        if name == "title" and parent == "note":
            self._text = []

    def characters(self, content: str) -> None:
        """Route character data to the payload decoder or a text buffer.

        Everything else -- most importantly the ``<content>`` CDATA
        section holding the note body, which can itself be large -- is
        dropped on the spot rather than accumulated.
        """
        if self._in_data:
            self._feed_base64(content)
        elif self._text is not None:
            self._text.append(content)

    def endElement(self, name: str) -> None:
        """Leave *name*, committing whatever it was capturing."""
        if self._stack:
            self._stack.pop()
        if name == "data" and self._in_data:
            self._end_data()
        elif self._text is not None:
            text = "".join(self._text).strip()
            self._text = None
            if name == "title":
                self._note_title = text
            elif name == "file-name":
                self._file_name = text
            elif name == "mime":
                self._mime = text
        if name == "resource" and self._resource_open:
            self._end_resource()
        elif name == "note":
            self._note_title = None

    # -- resource lifecycle -----------------------------------------------

    def _begin_resource(self) -> None:
        """Start a new ``<resource>``, resetting all per-resource state.

        The spool file is created here, before a single payload byte has
        been seen, because ``<data>`` precedes the ``<file-name>`` that
        decides the resource's fate: every resource is spooled, and
        ``</resource>`` then either renames that spool onto its final
        name or deletes it. Its *content* is never examined -- a
        presentation whose head is zero-filled or overwritten with
        foreign data is exactly the donor material the repair pipeline
        exists for, so judging payloads here would throw away the most
        valuable attachments.
        """
        self._resource_open = True
        self._resource_ordinal += 1
        self._file_name = None
        self._mime = None
        self._error = None
        self._discard = False
        self._carry = ""
        self._decoded = 0
        self._spool = None
        self._spool_path = None
        self._in_data = False
        self._open_spool()

    def _begin_data(self, encoding: str | None) -> None:
        """Start a ``<data>`` payload declared with *encoding*.

        A missing ``encoding`` attribute is taken as the base64 the
        format prescribes; any other value is a payload this module
        cannot decode, so the resource is failed right away rather than
        fed to the decoder.
        """
        self._in_data = True
        if encoding is not None and encoding.strip().lower() != "base64":
            self._fail(f"unsupported data encoding {encoding!r}")

    def _end_data(self) -> None:
        """Close a ``<data>`` payload, decoding any carried-over tail.

        A non-empty carry means the payload's length was not a multiple
        of four characters, i.e. it is malformed; the decode below then
        fails and the resource is dropped with a note, exactly like any
        other base64 damage.
        """
        self._in_data = False
        tail, self._carry = self._carry, ""
        if not tail or self._discard:
            return
        try:
            decoded = base64.b64decode(tail, validate=True)
        except ValueError as exc:
            self._fail(f"invalid base64 data: {exc}")
            return
        self._write(decoded)

    def _end_resource(self) -> None:
        """Close a ``<resource>``, keeping or dropping its spooled payload."""
        self._resource_open = False
        self._in_data = False
        if self._error is not None:
            self._discard_spool()
            self.pending.append(
                f"cannot read attachment {self._label()}: {self._error}")
            return
        file_name = self._accepted_name()
        if file_name is None:
            self._discard_spool()
            return
        self._materialize(file_name)

    def _accepted_name(self) -> str | None:
        """Return the file name to extract this resource under, or None.

        Returning None drops the resource silently -- it simply is not
        a PowerPoint attachment (or it is an Office ``~$`` owner/lock
        temp file, which is never a real presentation).
        """
        name = self._file_name or ""
        if name:
            if name.startswith(TEMP_PREFIX):
                return None
            if posixpath.splitext(name)[1].lower() in TARGET_SUFFIXES:
                return name
            return None
        # No file name recorded: fall back to the declared MIME type and
        # synthesize a name from the resource's position in the export.
        suffix = _PRESENTATION_MIMES.get((self._mime or "").lower())
        if suffix is None:
            return None
        return f"resource{self._resource_ordinal}{suffix}"

    # -- payload decoding -------------------------------------------------

    def _feed_base64(self, content: str) -> None:
        """Decode as much of *content* as forms whole base64 quadruples.

        Whitespace (the export's hard line wrapping) is removed first,
        then everything but a remainder of at most three characters is
        decoded; that remainder is carried over to the next call, which
        is what keeps memory flat regardless of the attachment's size.
        """
        if self._discard:
            return
        text = self._carry + content.translate(_WHITESPACE)
        usable = len(text) - len(text) % 4
        self._carry = text[usable:]
        if not usable:
            return
        try:
            decoded = base64.b64decode(text[:usable], validate=True)
        except ValueError as exc:
            # binascii.Error (a ValueError subclass) for a character
            # outside the alphabet or bad padding; a plain ValueError
            # when the text is not even ASCII.
            self._fail(f"invalid base64 data: {exc}")
            return
        self._write(decoded)

    def _write(self, data: bytes) -> None:
        """Append decoded *data* to this resource's spool file.

        Every resource's payload is written out verbatim, whatever it
        turns out to be; only ``</resource>`` decides whether the result
        is kept. ``_decoded`` therefore tracks exactly what reached the
        disk, which is what a kept attachment reports as its size.
        """
        if self._discard or self._spool is None or not data:
            return
        try:
            self._spool.write(data)
        except OSError as exc:
            self._fail(f"cannot spool attachment data: {exc}")
            return
        self._decoded += len(data)

    def _fail(self, reason: str) -> None:
        """Abandon this resource's payload and remember *reason* for a note."""
        self._discard = True
        self._error = reason
        self._discard_spool()

    # -- spool handling ---------------------------------------------------

    def _open_spool(self) -> None:
        """Create this resource's spool file under a unique temporary name.

        The spool lives inside the destination directory so the later
        rename onto the final name stays within one filesystem. A
        failure here (a destination directory that does not exist, a
        full or read-only filesystem) fails the resource rather than the
        whole export.
        """
        try:
            fd, raw_path = tempfile.mkstemp(dir=self._dest_dir,
                                             prefix=_SPOOL_PREFIX,
                                             suffix=_SPOOL_SUFFIX)
        except OSError as exc:
            self._fail(f"cannot spool attachment data: {exc}")
            return
        self._spool_path = Path(raw_path)
        try:
            self._spool = os.fdopen(fd, "wb")
        except OSError as exc:
            # Practically unreachable, but the descriptor must not leak
            # and the stray spool file must still be cleaned up.
            with contextlib.suppress(OSError):
                os.close(fd)
            self._fail(f"cannot spool attachment data: {exc}")

    def _close_spool(self) -> None:
        """Close the spool file handle, if one is open."""
        if self._spool is not None:
            with contextlib.suppress(OSError):
                self._spool.close()
            self._spool = None

    def _discard_spool(self) -> None:
        """Close and delete the spool file, leaving nothing behind."""
        self._close_spool()
        if self._spool_path is not None:
            # A removal failure would leave a stray ``.part`` file in the
            # caller-owned destination directory; there is nothing more
            # this module can do about it, and it must not abort a parse.
            with contextlib.suppress(OSError):
                self._spool_path.unlink(missing_ok=True)
            self._spool_path = None

    def _materialize(self, file_name: str) -> None:
        """Rename the spool onto its final name and queue the attachment.

        The payload is handed over exactly as it was decoded -- it is
        never inspected, so a head-damaged presentation reaches the
        diagnosis pipeline just like an intact one. The destination name
        is built exclusively from an acceptance ordinal and the
        *sanitized basename* of *file_name*
        (:func:`pptrepair.archive._dest_filename`), so a hostile
        ``<file-name>`` such as ``"../../etc/passwd"`` can never
        influence the directory half of the destination path. The name
        is then reserved with an exclusive ``"xb"`` create before the
        rename, so a collision -- impossible given the ordinal prefix,
        but guarded against anyway -- is reported instead of silently
        overwriting existing data.
        """
        label = self._label()
        if self._spool_path is None:
            # Unreachable: _begin_resource() always opens a spool and
            # reports the failure through _error, which _end_resource()
            # handles before ever getting here. Guarded anyway, because
            # no path through this handler may raise.
            self.pending.append(
                f"failed to extract attachment {label}: no spool file")
            return
        self._close_spool()

        member_name = f"{self._note_title or _UNTITLED}/{file_name}"
        dest_path = self._dest_dir / _dest_filename(self._accepted,
                                                     member_name)
        self._accepted += 1
        try:
            with dest_path.open("xb"):
                pass  # reserve the name atomically, then move onto it
        except FileExistsError:
            self.pending.append(
                f"destination name collision, attachment skipped: {label}")
            self._discard_spool()
            return
        except OSError as exc:
            self.pending.append(
                f"failed to extract attachment {label}: {exc}")
            self._discard_spool()
            return

        try:
            os.replace(self._spool_path, dest_path)
        except OSError as exc:
            dest_path.unlink(missing_ok=True)
            self.pending.append(
                f"failed to extract attachment {label}: {exc}")
            self._discard_spool()
            return

        self._spool_path = None
        member = ArchiveMember(archive_path=self._enex_path,
                                member_name=member_name,
                                size=self._decoded)
        self.pending.append((member, dest_path))

    # -- misc -------------------------------------------------------------

    def _label(self) -> str:
        """Return an ``"<enex>::<note title>/<file name>"`` label for notes."""
        name = self._file_name or f"resource{self._resource_ordinal}"
        return f"{self._enex_path}::{self._note_title or _UNTITLED}/{name}"

    def close(self) -> None:
        """Release any spool still open (aborted parse, cancelled caller)."""
        self._discard_spool()


def _drain(pending: list[_Pending],
           on_note: Callable[[str], None] | None
           ) -> Iterator[tuple[ArchiveMember, Path]]:
    """Yield the attachments queued so far, reporting queued notes in order.

    Items are popped one at a time so that an exception raised by
    *on_note* (cooperative cancellation) leaves the remaining queue
    intact rather than dropping it.
    """
    while pending:
        item = pending.pop(0)
        if isinstance(item, str):
            _emit(on_note, item)
        else:
            yield item


def iter_materialized_attachments(
    enex_path: Path,
    dest_dir: Path,
    *,
    on_note: Callable[[str], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Iterator[tuple[ArchiveMember, Path]]:
    """Extract *enex_path*'s PowerPoint attachments into *dest_dir*.

    The export is parsed incrementally, one :data:`_FEED_CHUNK` read at
    a time, and each ``.pptx``/``.pptm`` attachment is yielded as soon
    as its ``</resource>`` has been seen, as a
    ``(ArchiveMember, destination path)`` pair. The member's
    ``member_name`` is ``"<note title>/<file name>"`` (with
    :data:`_UNTITLED` standing in for a missing title) and its ``size``
    is the decoded payload size in bytes; the destination file lives
    directly under *dest_dir* and never inherits any directory
    component from the export. *dest_dir* must already exist; creating
    and cleaning it up is the caller's responsibility.

    A resource is taken when its ``<file-name>`` ends in ``.pptx`` or
    ``.pptm`` (case-insensitively), or -- when it carries no file name
    at all -- when its ``<mime>`` is one of the two PowerPoint
    presentation types, in which case it is extracted under a
    synthesized ``resourceN.pptx``/``.pptm`` name. Office ``~$``
    owner/lock temp files and every other kind of attachment are
    dropped silently. That decision rests on the recorded name and MIME
    type alone -- the payload itself is never inspected, exactly as in
    :func:`pptrepair.archive.materialize`, so an attachment whose head
    is zero-filled or overwritten with foreign data is still extracted
    and left for the diagnosis pipeline to judge.

    *on_note* (when given) receives one English message per resource
    that looked like a presentation but could not be extracted (damaged
    base64, an undecodable encoding, a failed write), and one final
    message if the export itself cannot be opened or parsed. A parse
    failure ends the iteration, but everything found before it has
    already been yielded.

    *progress* (when given) is called once per read chunk with the
    number of bytes fed to the parser so far and the export's total
    size, so a caller can drive a progress bar over a very large file.

    Cooperative cancellation: exceptions raised by *on_note* or
    *progress* are the only ones this function lets escape -- they
    propagate unmodified, which is the documented way to abort a run
    (see :class:`pptrepair.cancel.OperationCancelled`). Any spool file
    still open at that point is removed on the way out.
    """
    try:
        stream = enex_path.open("rb")
    except OSError as exc:
        _emit(on_note, f"cannot read enex {enex_path}: {exc}")
        return

    try:
        total = enex_path.stat().st_size
    except OSError:
        total = 0

    handler = _AttachmentHandler(enex_path, dest_dir)
    failure: str | None = None
    fed = 0

    try:
        with stream:
            try:
                parser = xml.sax.make_parser()
                # Evernote writes a DOCTYPE pointing at a DTD on
                # evernote.com; with external general entities disabled
                # the parser can never be talked into fetching it (or
                # any other external resource), which rules out both
                # network access and the classic XXE file reads.
                parser.setFeature(xml.sax.handler.feature_external_ges,
                                  False)
                parser.setContentHandler(handler)
            except Exception as exc:
                _emit(on_note, f"cannot parse enex {enex_path}: {exc}")
                return

            while True:
                try:
                    chunk = stream.read(_FEED_CHUNK)
                except OSError as exc:
                    failure = f"cannot read enex {enex_path}: {exc}"
                    break
                if not chunk:
                    break
                try:
                    parser.feed(chunk)
                except Exception as exc:
                    # Deliberately broad: a malformed export surfaces as
                    # SAXParseException, but a damaged encoding
                    # declaration or an exotic expat failure can surface
                    # as other types, and this boundary must not raise.
                    failure = f"cannot parse enex {enex_path}: {exc}"
                    break
                fed += len(chunk)
                # Drain before reporting progress so everything already
                # on disk reaches the caller even if *progress* cancels.
                yield from _drain(handler.pending, on_note)
                if progress is not None:
                    progress(fed, total)

            if failure is None:
                try:
                    parser.close()
                except Exception as exc:
                    failure = f"cannot parse enex {enex_path}: {exc}"

        # Whatever the last feed (or close) completed is still queued.
        yield from _drain(handler.pending, on_note)
        if failure is None and fed == 0:
            failure = f"cannot parse enex {enex_path}: file is empty"
        if failure is not None:
            _emit(on_note, failure)
    finally:
        handler.close()
