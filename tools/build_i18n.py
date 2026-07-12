"""Compile ``.po`` translation catalogs into GNU gettext ``.mo`` binaries.

This is a development-time tool with no third-party dependencies: it
scans ``pptrepair/locale/<lang>/LC_MESSAGES/pptrepair.po`` files, parses
the (small) subset of the PO format used by this project, and writes a
sibling ``pptrepair.mo`` file next to each one. The compiled ``.mo``
files are what :func:`pptrepair.i18n.get_translator` loads at runtime;
this script is *not* part of the installed package.

Usage::

    python tools/build_i18n.py
    python tools/build_i18n.py --locale-dir /path/to/locale

Supported PO subset:

* Comment lines (``#...``) are ignored.
* ``msgid "..."`` / ``msgstr "..."`` entries, each optionally followed
  by continuation lines consisting solely of another quoted string
  (``"..."``), which are concatenated onto the previous value.
* Escape sequences ``\\n``, ``\\t``, ``\\"`` and ``\\\\`` inside quoted
  strings.
* The header entry (``msgid ""`` with metadata packed into
  ``msgstr``), stored like any other entry.

Plural forms (``msgid_plural``) are *not* supported; this project's
message conventions avoid plurals entirely (see the module docstring
of :mod:`pptrepair.i18n`), so encountering one is treated as a PO file
error rather than being silently mistranslated.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

#: .mo file magic number for little-endian catalogs.
_MO_MAGIC = 0x950412DE

#: .mo file format revision written by this compiler.
_MO_REVISION = 0

#: Default directory tree scanned for ``<lang>/LC_MESSAGES/pptrepair.po``.
DEFAULT_LOCALE_DIR = (
    Path(__file__).resolve().parent.parent / "pptrepair" / "locale"
)

#: gettext domain, must match ``pptrepair.i18n.DOMAIN``.
DOMAIN = "pptrepair"


def _unescape(quoted: str) -> str:
    """Strip the surrounding quotes from *quoted* and resolve escapes.

    :param quoted: a single PO string literal, including its
        surrounding double quotes (e.g. ``'"foo\\nbar"'``).
    :raises ValueError: when *quoted* is not a well-formed quoted
        string.
    """
    if len(quoted) < 2 or quoted[0] != '"' or quoted[-1] != '"':
        raise ValueError(f"malformed quoted string: {quoted!r}")
    body = quoted[1:-1]

    chars: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body):
            escape = body[i + 1]
            if escape == "n":
                chars.append("\n")
            elif escape == "t":
                chars.append("\t")
            elif escape == '"':
                chars.append('"')
            elif escape == "\\":
                chars.append("\\")
            else:
                # Unknown escape: keep the escaped character verbatim.
                chars.append(escape)
            i += 2
        else:
            chars.append(char)
            i += 1
    return "".join(chars)


def parse_po(text: str) -> dict[str, str]:
    """Parse *text* (the contents of a ``.po`` file) into a msgid/msgstr map.

    Only the PO subset documented in this module's docstring is
    supported; see there for details.

    :raises ValueError: on a ``msgid_plural`` entry, an unrecognized
        line, or a malformed quoted string.
    """
    catalog: dict[str, str] = {}
    msgid: str | None = None
    msgstr: str | None = None
    current: str | None = None  # either "msgid" or "msgstr"

    def flush() -> None:
        # Commit the entry accumulated so far, if any, to the catalog.
        if msgid is not None and msgstr is not None:
            catalog[msgid] = msgstr

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("msgid_plural"):
            raise ValueError(
                "plural forms (msgid_plural) are not supported by "
                "this project's PO subset"
            )
        if line.startswith("msgid "):
            flush()
            msgid = _unescape(line[len("msgid "):].strip())
            msgstr = None
            current = "msgid"
        elif line.startswith("msgstr "):
            msgstr = _unescape(line[len("msgstr "):].strip())
            current = "msgstr"
        elif line.startswith('"'):
            # Continuation line: append to whichever field is active.
            if current == "msgid" and msgid is not None:
                msgid += _unescape(line)
            elif current == "msgstr" and msgstr is not None:
                msgstr += _unescape(line)
            else:
                raise ValueError(
                    f"continuation line outside an entry: {raw_line!r}"
                )
        else:
            raise ValueError(f"unrecognized PO line: {raw_line!r}")
    flush()
    return catalog


def pack_mo(catalog: dict[str, str]) -> bytes:
    """Serialize *catalog* (msgid -> msgstr) into GNU gettext ``.mo`` bytes.

    Strings are UTF-8 encoded and the original-string table is written
    in sorted order, matching the layout GNU ``msgfmt`` produces.
    """
    # Original strings must be sorted so tools that binary-search the
    # catalog (e.g. GNU libintl) can find entries correctly.
    msgids = sorted(catalog)

    encoded_ids = [msgid.encode("utf-8") for msgid in msgids]
    encoded_strs = [catalog[msgid].encode("utf-8") for msgid in msgids]

    count = len(msgids)
    header_size = 7 * 4
    id_table_size = count * 8
    str_table_size = count * 8

    ids_start = header_size + id_table_size + str_table_size
    ids_blob = b"\x00".join(encoded_ids) + (b"\x00" if encoded_ids else b"")
    strs_start = ids_start + len(ids_blob)

    id_table = bytearray()
    offset = ids_start
    for chunk in encoded_ids:
        id_table += struct.pack("<II", len(chunk), offset)
        offset += len(chunk) + 1  # +1 for the terminating NUL

    str_table = bytearray()
    offset = strs_start
    strs_blob = bytearray()
    for chunk in encoded_strs:
        str_table += struct.pack("<II", len(chunk), offset)
        strs_blob += chunk + b"\x00"
        offset += len(chunk) + 1

    header = struct.pack(
        "<7I",
        _MO_MAGIC,
        _MO_REVISION,
        count,
        header_size,  # offset of the original-strings table
        header_size + id_table_size,  # offset of the translated-strings table
        0,  # hash table size (unused: no hash table is written)
        0,  # hash table offset (unused)
    )
    return (
        bytes(header) + bytes(id_table) + bytes(str_table)
        + ids_blob + bytes(strs_blob)
    )


def compile_catalog(po_path: Path) -> int:
    """Compile a single ``.po`` file into a sibling ``.mo`` file.

    :param po_path: path to the ``.po`` source file; the ``.mo`` file
        is written next to it with the same stem.
    :returns: the number of translatable messages compiled, excluding
        the header entry (``msgid ""``).
    """
    catalog = parse_po(po_path.read_text(encoding="utf-8"))
    mo_path = po_path.with_suffix(".mo")
    mo_path.write_bytes(pack_mo(catalog))
    return len(catalog) - (1 if "" in catalog else 0)


def compile_all(locale_dir: Path) -> list[tuple[str, int]]:
    """Compile every catalog under *locale_dir* and return per-language counts.

    :param locale_dir: root directory containing
        ``<lang>/LC_MESSAGES/pptrepair.po`` files.
    :returns: a list of ``(lang, message_count)`` pairs, ordered by
        language code.
    """
    results: list[tuple[str, int]] = []
    pattern = f"*/LC_MESSAGES/{DOMAIN}.po"
    for po_path in sorted(locale_dir.glob(pattern)):
        lang = po_path.parent.parent.name
        count = compile_catalog(po_path)
        results.append((lang, count))
    return results


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Compile pptrepair .po catalogs into .mo binaries."
    )
    parser.add_argument(
        "--locale-dir",
        type=Path,
        default=DEFAULT_LOCALE_DIR,
        help="root directory containing <lang>/LC_MESSAGES/pptrepair.po "
             "files (default: the pptrepair package's locale directory)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    for lang, count in compile_all(args.locale_dir):
        print(f"{lang}: {count} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
