# PPTrepair

Diagnoses and repairs PowerPoint files that were corrupted while stored on OneDrive — from the command line or a desktop GUI.

日本語版は [README_ja.md](README_ja.md) を参照してください。 (For the Japanese version, see [README_ja.md](README_ja.md).)

## What it does

`pptrepair check` inspects `.pptx` files and reports whether each one is an intact PowerPoint package or matches one of the OneDrive corruption patterns identified by this project. Real-world analysis showed that these corruptions are chunk-wise overwrites or truncations aligned to 256 KiB / 1 MiB boundaries, falling into distinct patterns:

| Verdict | Meaning |
|---|---|
| `normal` | intact PowerPoint package (every entry readable) |
| `head_zero_fill` | leading chunks overwritten with zeros; only the file tail survives |
| `head_foreign_data` | leading chunks overwritten with unrelated data |
| `version_mix` | a collage of chunks from two different save versions |
| `tail_truncated` | file cut off prematurely; the ZIP central directory is lost |
| `interior_damage` | interior entries damaged while the file head and ZIP index survive |
| `tail_foreign_data` | a complete archive with foreign data appended after it, hiding its ZIP index |
| `full_zero_fill` | (almost) the whole file overwritten with zeros; nothing survives |
| `empty_file` | zero-byte file; nothing survives |
| `other_corrupt` | damaged, but not matching a known pattern |
| `not_a_zip` | not a ZIP-based file at all |

For corrupted files the report also shows how much content is salvageable (entries and slides), which indicates what a future repair could recover. All files are opened read-only — this tool never modifies your data.

## Installation

Requires Python 3.12 or later. No runtime dependencies beyond the standard library. Pick whichever workflow matches your Python setup.

### pipx (recommended for CLI tools)

Installs `pptrepair` as an isolated, globally available command:

```console
$ pipx install git+https://github.com/xhighhongo41/PPTrepair.git
$ pptrepair check presentation.pptx
```

From a local clone, `pipx install .` works the same way.

### uv

```console
$ uv tool install git+https://github.com/xhighhongo41/PPTrepair.git
$ pptrepair check presentation.pptx
```

Inside a clone you can also run it without installing anything:

```console
$ uv run pptrepair check presentation.pptx
```

### pip + venv (Python standard tooling)

```console
$ git clone https://github.com/xhighhongo41/PPTrepair.git
$ cd PPTrepair
$ python -m venv .venv
$ source .venv/bin/activate    # Windows: .venv\Scripts\activate
(.venv) $ pip install .
(.venv) $ pptrepair check presentation.pptx
```

### pipenv

```console
$ git clone https://github.com/xhighhongo41/PPTrepair.git
$ cd PPTrepair
$ pipenv install -e .
$ pipenv run pptrepair check presentation.pptx
```

### pyenv / conda users

pyenv manages Python versions rather than environments: make sure `python` resolves to 3.12+ (e.g. `pyenv install 3.13 && pyenv shell 3.13`), then use any of the methods above. With conda, create an environment first: `conda create -n pptrepair python=3.13 && conda activate pptrepair && pip install .`

## Usage

```console
$ pptrepair check presentation.pptx [more.pptx ...]
$ pptrepair check --json presentation.pptx   # machine-readable output
$ pptrepair check --lang ja presentation.pptx   # report language
```

Example output for a truncated file:

```
=== presentation.pptx ===
Verdict: tail_truncated (corrupted: file tail truncated)
Evidence:
  - no end-of-central-directory record found
  - file starts with a local file header signature
  - 192 local file header(s) found
Salvageable: 190/192 entries, 37/37 slides
```

Exit codes: `0` — every file is intact, `1` — at least one file is corrupted, `2` — usage or I/O error.

On structurally intact files, `check` additionally runs three content-integrity inspections and reports what they find — the kinds of inconsistency that make PowerPoint offer a one-time repair on first open — without changing the verdict or the exit code: unresolved XML relationship references, animation/timing nodes pointing at shapes that no longer exist or no longer carry their media, and missing required structural relationships (such as a slide master left without a theme). `--json` carries the details under `xml_ref_integrity`, `timing_integrity` and `structure_integrity`. This also spots files repaired by PPTrepair 1.1.1 or earlier, whose rebuilds could leave such inconsistencies behind.

### Repairing

```console
$ pptrepair repair broken.pptx                 # automatic strategy
$ pptrepair repair broken.pptx -o fixed.pptx   # explicit output path
$ pptrepair repair broken.pptx --lang ja       # report language
```

`repair` never modifies the input file. Depending on the damage it produces one of:

* **a rebuilt presentation** `<name>.repaired.pptx` — when the surviving data still contains the slides (tail-truncated, version-mixed and interior-damaged files). Every surviving slide is recovered. When images or media were lost with the damage, the rebuild also removes the now-dangling references to them — a video whose media stream was lost keeps its poster frame as a still picture — so PowerPoint opens the result cleanly instead of offering to repair it.
* **a trimmed presentation** `<name>.repaired.pptx` — when a complete archive is hiding behind appended foreign data (`tail_foreign_data`): the appended bytes are cut off to recover the original archive byte-for-byte, falling back to a rebuild when the leading archive is itself damaged.
* **a recovery folder** `<name>.salvaged/` — when the slide bodies themselves were destroyed. Surviving pictures land in `images/`, audio/video in `media/`, best-effort recovered text (slide titles, document metadata) in `texts/`, chart data in `charts/`, every raw part in `parts/`, plus a human-readable `REPORT.txt` stating exactly what was lost.

The report language is selectable with `--lang` (`en`, `ja`, `zh`, `ko`, `es`, `fr`, `de`; default English). An existing output is never overwritten unless you pass `--force`; `--json` gives machine-readable results. Exit codes: `0` — artifact produced (or the file was already intact), `1` — nothing recoverable, `2` — usage or I/O error.

### Scanning whole folder trees

```console
$ pptrepair scan ~/OneDrive/Documents            # find corrupted files
$ pptrepair scan ~/slides --report scan_out      # also save reports
```

`scan` walks each directory recursively, diagnoses every `.pptx` / `.pptm` file it finds (read-only, nothing is written without `--report`), prints corrupted files as they are found, and ends with a summary:

```
Projects/deck1.pptx: head_zero_fill
Projects/old/deck2.pptx: tail_truncated
=== Scan summary ===
Scanned: 16 file(s)
  normal: 14
  head_zero_fill: 1
  tail_truncated: 1
```

Useful to know:

* **Cloud-only files are never downloaded by default.** Placeholder files that exist only in the cloud (OneDrive Files On-Demand, iCloud Drive, and other clients built on the OS-standard placeholder mechanisms) are detected from metadata alone and skipped; the summary always states how many files were left unexamined. Only PowerPoint files count here — everything else is filtered out by name first, so a folder full of cloud-only photos or documents does not inflate the number. Pass `--allow-download` to have them downloaded and examined too — each file is announced on stderr as its download starts, so long scans stay visible (this may take long and use significant disk space). Clients with proprietary placeholder implementations — notably Google Drive for desktop on Windows — cannot be detected this way, so reading their files may still trigger a download.
* Encrypted or legacy binary Office files (OLE compound documents) are recognized and reported as such rather than as corruption. Legacy `.ppt` and Office `~$` lock files are counted and skipped; symbolic links are ignored unless you pass `--follow-symlinks`.
* **`--search-archives` also mines backup archives for restore material.** Zip and tar archives (`.zip`, `.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`) found during the walk are opened and every `.pptx`/`.pptm` inside is diagnosed — an intact twin or an older version of a corrupted file kept inside a backup then shows up among the restore/lineage/merge candidates, labelled `backup.zip::inner/deck.pptx`. Archived files are donor material only: they are never repaired, never counted as scanned or corrupted, and members are extracted one at a time to a temporary directory that is removed afterwards. Cloud-only archives follow the same rule as cloud-only PowerPoint files (skipped unless `--allow-download`). With the flag, the report JSON carries `schema_version: 4` and an `origin_archive` key on archive-derived candidates; without it, output is unchanged.
* `--max-file-size SIZE` skips files larger than SIZE before diagnosing them (a plain byte count, or with a `K`/`M`/`G`/`T` suffix, e.g. `500M`, `2G`); unlimited by default.
* `--report DIR` writes `scan_report.txt`, machine-readable `scan_report.json`, and — for unknown corruption patterns — anonymous diagnostic fingerprints (see below). An existing report directory needs `--force`. `--lang` and `--json` work like in `repair`.
* Exit codes: `0` — everything examined is intact, `1` — corruption found, `2` — some paths could not be examined.

### Repairing whole folder trees at once

```console
$ pptrepair repair-all ~/OneDrive/Documents -o ~/repaired    # aggregate output
$ pptrepair repair-all ~/slides --in-place                   # next to each source
$ pptrepair repair-all ~/slides -o ~/repaired --dry-run      # plan only, write nothing
```

`repair-all` combines `scan` and `repair`: it walks each directory recursively (with the same cloud-placeholder safety as `scan`), then repairs every corrupted file it found, streaming one line per file and ending with a repair summary.

* **Aggregate output (`-o OUTDIR`)** mirrors the input tree structure under `OUTDIR`, so nothing is ever written into the scanned folders — the safe choice for a live OneDrive tree. With several DIRs, each gets its own subdirectory under `OUTDIR` (named after the root, numbered when names clash).
* **`--in-place`** writes each artifact next to its own source instead, like a per-file `pptrepair repair` run. Inside a synced folder the artifacts are synced too, and a later re-run will scan them as ordinary (intact) files.
* Artifacts follow the single-file conventions: a rebuilt/trimmed `<name>.repaired.pptx` or a `<name>.salvaged/` recovery folder (with its `REPORT.txt`). An artifact that already exists is skipped — so an interrupted batch can simply be re-run — unless you pass `--force`. One file's failure never stops the rest.
* Encrypted/legacy Office files are reported but never attempted; files with no surviving content (empty or fully zeroed) are honestly reported as unrepairable.
* `--dry-run` diagnoses everything and prints the repair plan without writing anything at all (not even reports).
* `--max-file-size SIZE` skips files larger than SIZE before diagnosing or repairing them (a plain byte count, or with a `K`/`M`/`G`/`T` suffix, e.g. `500M`, `2G`); unlimited by default.
* `--report DIR` writes `scan_report.txt`/`.json`, `repair_report.txt`/`.json` and anonymous diagnostic fingerprints for unknown patterns, like `scan --report`.
* Exit codes: `0` — no corruption found, or every corrupted file was repaired; `1` — at least one corrupted file was left unrepaired (unrepairable, or skipped over an existing artifact); `2` — some paths could not be examined, or a repair failed with an error.

### Salvaging parts of unrepairable files

```console
$ pptrepair salvage hopeless.pptx              # rescue whatever survives
$ pptrepair salvage hopeless.pptx -o rescued   # explicit output folder
```

When a file is too damaged even for `repair` — for instance when a huge leading block was overwritten in place — `salvage` pulls out whatever content still survives, without ever modifying the input. It writes a `<name>.rescued/` folder containing:

* `entries/` — every package part that can still be read back intact (verified against its stored checksum);
* `carved/` — JPEG/PNG images recovered straight from the raw bytes by signature carving. This works even when the ZIP structure is gone entirely — it can even pull images out of the foreign data that overwrote the file, which is why the report marks carved images' provenance as unknown;
* `partial_xml/` — the readable leading part of damaged XML parts, decompressed up to the first broken byte;
* `rescued_text.txt` — best-effort text recovered from surviving and partially recovered slide XML;
* `salvage_report.json` — a machine-readable inventory of everything above.

`--lang`, `--json` and `--force` work like in `repair`. Exit codes: `0` — something was rescued (or the file was already intact), `1` — nothing could be rescued, `2` — usage or I/O error.

### Restore candidates from intact twins

OneDrive-style chunk corruption overwrites bytes in place and preserves the file size, so an intact copy of the same presentation elsewhere in the scanned tree — a stray copy, an old export, a sync-conflict sibling — is often the fastest full restore. The reports written by `scan --report` list, under every corrupted file, intact files that share its name and/or exact byte size:

```
  - Projects/deck1.pptx: head_foreign_data
  restore candidate: Archive/deck1.pptx (same name and size)
```

`scan_report.json` carries the same data under `twin_candidates` (with a `high` / `medium` / `low` confidence per candidate), and `repair-all` reports list candidates for every file left unrepaired; its `repair_report.json` `schema_version` is now 2. With `--search-archives`, intact twins kept inside backup archives are listed too, labelled `backup.zip::deck.pptx`. PPTrepair only points at candidates — verify their content before replacing anything by hand.

### Merging several corrupted copies into one restored file

```console
$ pptrepair merge main.pptx conflict-copy.pptx            # two or more copies
$ pptrepair merge main.pptx copy2.pptx old-version.pptx --yes
$ pptrepair merge main.pptx backup-2023.zip               # donors from a backup archive
```

OneDrive corruption often leaves several differently-damaged copies of the same presentation behind — the working file plus a sync-conflict copy, for example. `merge` reconstructs one file from all of them: the first argument is the file being restored, every further argument is a possible donor. Each archive entry is taken from whichever copy still reproduces the CRC-32 checksum the file's own index recorded for it, so a wrong byte can never be adopted silently; when no single copy holds an entry whole, the pieces are recombined across the 64 KiB boundaries at which this corruption operates.

Sources are vetted before use. An exact same-size copy is used automatically when its index matches closely; a same-size copy with a weaker match, or a *different version* of the same presentation (recognised mainly by byte-identical embedded media), is only used after an interactive confirmation that shows the evidence (`--allow-candidate` / `--yes` skip the prompts, e.g. for scripts).

Any SRC after the first may also be a backup archive (`.zip`, `.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`): every `.pptx`/`.pptm` inside is extracted to a temporary directory and vetted exactly like a plain file, and is named `backup.zip::inner/deck.pptx` in prompts and reports (`--json` additionally records each source's `origin_archive`). Encrypted, damaged or unreadable members are skipped with a note. The file being restored itself cannot live inside an archive — extract it first.

The result reports its guarantee level:

* `full` — every entry verified; the output is **byte-identical to the original file**;
* `partial` — everything that could be verified was restored and repackaged; nothing unverified was included;
* `hybrid` — some parts were filled in from a different version of the presentation and are *not* guaranteed to match the lost original (a warning lists them);
* `failed` — nothing usable could be reconstructed (exit code 1).

`scan --report` and `repair-all` reports point out merge opportunities: corrupted files sharing an exact byte size are listed as a merge group with a ready-to-run command, and likely other versions appear as `lineage_candidates` with their similarity score. `repair_report.json` `schema_version` is now 3.

### Reporting unknown corruption patterns

When `scan` meets damage that matches no known pattern (`other_corrupt`, or a `not_a_zip` that is not an encrypted/legacy Office file), running it with `--report` writes one `diagnostics/<id>.diag.json` fingerprint per affected file (at most 20 per run). A fingerprint contains **structural information only** — byte offsets, an entropy profile, ZIP statistics and standardized OOXML part names — never your document's text, images, file name or path. The file's basename is included only if you opt in with `--include-filenames`.

If you hit an unknown pattern, please review the fingerprint file yourself and consider attaching it to a [new issue](https://github.com/xhighhongo41/PPTrepair/issues/new/choose) using the *Unknown corruption pattern report* template. These reports are what future repair strategies get built from.

### Desktop GUI (v2.0)

For everyday use without a terminal, `pptrepair gui` launches a PySide6-based desktop application that wraps the same scan/repair engine as the CLI.

```console
$ pipx install "pptrepair[gui] @ git+https://github.com/xhighhongo41/PPTrepair.git"
$ pptrepair gui
```

The GUI needs the optional `[gui]` extra (PySide6) on top of the plain install described above; from a local clone, `pip install '.[gui]'` works the same way.

Drop files, folders (scanned recursively for `.pptx`/`.pptm`) or backup archives onto the window — repeated drops keep accumulating into one work set. **Scan** diagnoses everything with live progress and a Cancel button, then lists the results in a **Files** tab (colour-coded verdicts) and a **Candidates** tab (the same restore/lineage/merge candidates `scan --report` writes to disk). **Repair** then runs in one of two modes:

* **Single-file** repairs each corrupted file from its own bytes only, like `repair`/`repair-all`.
* **Multi-source** merges in donor material mined from other copies and from any dropped backup archives, like `merge` — a donor-approval dialog shows the evidence for every candidate and lets you check or uncheck it before repairing; verified byte-identical donors are pre-checked, while the weaker candidate/lineage matches start unchecked and need deliberate approval, mirroring the CLI's own trust model.

Output goes either **in place** next to each source, or into a chosen folder that mirrors the input tree — the GUI equivalents of `--in-place` and aggregate `-o OUTDIR`. A Preferences dialog controls whether cloud-only files may be downloaded, the maximum file size examined (default 2 GB, the GUI equivalent of `--max-file-size`), and the UI/report language — the same 7 languages as the CLI, with a change taking effect after restart. Every setting persists across launches. As with the CLI, dropped archives are only donor material: the files inside them are never repaired themselves.

## Changelog

### ver 2.0.0 (2026-07-25)
- New PySide6 desktop GUI (`pptrepair gui`, optional `[gui]` extra): drag & drop accumulation, recursive folder scan, live progress with cancellation, single-file / multi-source repair modes with donor approval, archive donor mining, in-place / mirrored folder output, persistent settings, 7-language UI
- New `--max-file-size` option for `scan` / `repair-all` skips files above a given size (e.g. `500M`, `2G`; default: no limit)
- Core: a cooperative cancellation contract (`OperationCancelled`) and a per-member archive-mining progress callback, both added to support the GUI's live progress and Cancel button
- Development: adopted ruff for linting; `tools/check.sh` now runs ruff and the test suite as one pipeline

### ver 1.3.1 (2026-07-23)
- Backup archives can now supply restore material. `pptrepair merge` accepts zip/tar archives (`.zip`, `.tar`, `.tar.gz`, `.tar.bz2`, `.tar.xz` and their short forms) as additional SRC arguments, and `scan` / `repair-all` gained an opt-in `--search-archives` flag that mines archives found during the walk — an intact twin or an older version kept inside a backup shows up among the restore/lineage/merge candidates, labelled `backup.zip::inner/deck.pptx`. Archived files are donor material only: never repaired, never counted, and cloud-only archives are never downloaded without `--allow-download`. Verified against real corrupted files: byte-identical `full` restores from a twin inside a zip and inside a tar.gz
- `check` now supports `--lang` like every other command
- Internal: `cli.py` / `report.py` split into per-command modules (no behavior change); translation template refreshed

### ver 1.3 (2026-07-23)
- New `pptrepair merge` command reconstructs one restored file from any number of differently-damaged copies of the same presentation. Every adopted byte range is verified against the CRC-32 recorded by the file's own index (`full` results are byte-identical to the original — proven against a real intact twin), entries surviving in no copy whole are recombined across the 64 KiB corruption boundaries, and *different versions* of the same presentation can donate parts after an interactive confirmation (`hybrid` results clearly mark what came from another version). Donor parts are matched by content, not by part name, so slides and media renumbered by an insertion between versions are still found and restored to their correct position
- `scan --report` / `repair-all` reports now list merge groups (same-size corrupted files with a ready-to-run `merge` command) and lineage candidates (likely other versions of a corrupted file, scored by shared embedded media). `repair_report.json` `schema_version` is now 3
- Repaired and merged outputs are now also self-checked for *orphaned* slide/notes parts — a fourth real-world trigger of PowerPoint's repair dialog, discovered (and fixed) during v1.3 acceptance testing
- Package version is now defined in one place (`pptrepair.__version__`) via setuptools dynamic metadata

### ver 1.2.1.1 (2026-07-22)
- Fixed `pptrepair --version` (and the `tool_version` field of diagnostic fingerprints) still reporting 1.2.0: the package version string had not been bumped in the 1.2.1 release. No functional changes

### ver 1.2.1 (2026-07-22)
- New `pptrepair salvage` command rescues content from files too damaged to repair: intact package entries, JPEG/PNG images carved from the raw bytes (even out of the foreign data that overwrote the file), the readable leading part of damaged XML, best-effort text, and a machine-readable `salvage_report.json`
- `scan --report` and `repair-all` reports now list **restore candidates**: intact files elsewhere in the scanned tree that share a corrupted file's name and/or exact byte size (this kind of corruption preserves file size, so such a twin is often a full restore). `repair_report.json` `schema_version` is now 2
- Better classification of massive head overwrites: real-world specimens showed the overwriting data can itself contain CRC-valid fragments of *another* ZIP archive, which fooled the `head_foreign_data` detector into `other_corrupt`. The detector now cross-checks scanned entries against the central directory, names the foreign fragments in its evidence, and two new fallback verdicts (`foreign_zip_overwrite`, `scattered_overwrite`) classify related damage that is not confined to the head

### ver 1.2 (2026-07-19)
- Added the `pptrepair repair-all` command: recursively scans one or more directory trees and repairs every corrupted `.pptx` / `.pptm` found — either into an aggregate output directory that mirrors the input tree (`-o OUTDIR`, never writing into the scanned folders) or next to each source (`--in-place`). Streams per-file progress and ends with a repair summary; same cloud-placeholder safety as `scan`
- `--dry-run` prints the full repair plan without writing anything; `--report DIR` saves `scan_report.txt`/`.json`, `repair_report.txt`/`.json` and anonymous diagnostic fingerprints for unknown patterns
- Batches are resumable: existing artifacts are skipped unless `--force`, and one file's failure never stops the rest. Encrypted/legacy Office files are reported but not attempted

### ver 1.1.2 (2026-07-17)
- Rebuilt presentations now open cleanly, with no PowerPoint repair prompt. Verified against a real-world corpus, `repair` now resolves all three prompt triggers it could leave behind: dangling relationship references in slide XML are removed (a video whose media stream was lost keeps its poster frame as a still picture), the animation/timing tree is reconciled with the shapes that lost their media or were removed, and a default Office theme is synthesized when the damage carried away a theme that a surviving slide master still references
- `check` gains three content-integrity inspections for structurally intact files: unresolved relationship references, inconsistent timing references, and missing required structural relationships — reported (also under `xml_ref_integrity` / `timing_integrity` / `structure_integrity` in `--json`) without changing the verdict or exit code; this also detects files repaired by earlier releases
- `repair` re-verifies its own artifact with the same three inspections and reports the results; trim artifacts stay byte-identical to the original archive, so pre-existing inconsistencies there are reported but left untouched

### ver 1.1.1 (2026-07-17)
- Added four verdicts for corruption geometries identified from the first collected diagnostic fingerprints: `interior_damage`, `tail_foreign_data`, `full_zero_fill` and `empty_file`
- `repair` gains a trim strategy: a `tail_foreign_data` file is recovered byte-for-byte by cutting the appended foreign data off (falling back to a salvage rebuild when the leading archive is itself damaged); `interior_damage` files repair through the existing rebuild path
- More sensitive head-damage detection: `head_zero_fill` now triggers from 4 KiB of leading zeros (previously 64 KiB), and `head_foreign_data` is recognized even when the foreign data starts with zero bytes
- Empty and fully zero-filled files no longer consume diagnostic-fingerprint slots and receive honest "nothing survives" guidance instead of unknown-pattern prompts
- Known limitation: when a repair recovers slides whose images were lost, the rebuilt file still references those now-missing pictures, so PowerPoint may offer a one-time repair on first open; automatic cleanup of the stale references is planned for the next release

### ver 1.1 (2026-07-13)
- Added the `pptrepair scan` command: recursively sweeps directory trees for corrupted `.pptx` / `.pptm` files, streaming per-file verdicts and ending with a summary (all 7 report languages supported)
- Cloud-only placeholder files (OneDrive Files On-Demand, iCloud Drive and other OS-standard placeholder mechanisms) are detected from metadata and skipped without triggering downloads; `--allow-download` opts in, and every scan discloses how many files were left unexamined
- Opt-in `--report` folder with `scan_report.txt` / `scan_report.json` and anonymous, shareable diagnostic fingerprints (versioned schema, no document content) for unknown corruption patterns, plus a GitHub issue template for submitting them
- Encrypted and legacy binary Office files (OLE compound documents) are now recognized and no longer look like corruption candidates

### ver 1.0 (2026-07-12)
- Added the `pptrepair repair` command: rebuilds a consistent, openable .pptx from tail-truncated and version-mixed files, or produces a recovery folder (images, media, recovered text, chart data, raw parts and a damage report) when the slide bodies are unrecoverable
- Human-readable reports in 7 languages via `--lang` (en, ja, zh, ko, es, fr, de), implemented with standard GNU gettext catalogs
- The input file is never modified; existing outputs are only overwritten with `--force`

### ver 0.2 (2026-07-12)
- Added the `pptrepair check` command, which classifies `.pptx` files as intact or as one of the known OneDrive corruption patterns, with evidence and a salvageability summary
- Text and `--json` output, scripting-friendly exit codes
- Installable Python package (`pip install .`), standard library only

### ver 0.1 (2026-07-12)
- Researched publicly reported cases of PowerPoint (.pptx) file corruption on OneDrive, and analyzed the binary structure of actual corrupted files
- Confirmed that the corruption is not random bit rot, but chunk-wise overwriting/truncation aligned to 256 KiB / 1 MiB boundaries, and that it falls into several distinct patterns (leading zero-fill, leading foreign data, old-version mixture, and tail truncation)
- Verified that for some patterns most slides can be salvaged by scanning the data remaining in the file, and formulated the implementation strategy for the repair tool
- Decided on Python (standard library only) as the implementation language
