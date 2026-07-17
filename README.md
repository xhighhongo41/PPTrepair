# PPTrepair

Diagnoses and repairs PowerPoint files that were corrupted while stored on OneDrive.

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

### Repairing

```console
$ pptrepair repair broken.pptx                 # automatic strategy
$ pptrepair repair broken.pptx -o fixed.pptx   # explicit output path
$ pptrepair repair broken.pptx --lang ja       # report language
```

`repair` never modifies the input file. Depending on the damage it produces one of:

* **a rebuilt presentation** `<name>.repaired.pptx` — when the surviving data still contains the slides (tail-truncated, version-mixed and interior-damaged files). Every surviving slide is recovered. When some images were lost with the damage, the rebuilt file still opens, but PowerPoint may offer to repair it once to clear the now-missing picture references; a future release will remove those stale references automatically.
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
* `--report DIR` writes `scan_report.txt`, machine-readable `scan_report.json`, and — for unknown corruption patterns — anonymous diagnostic fingerprints (see below). An existing report directory needs `--force`. `--lang` and `--json` work like in `repair`.
* Exit codes: `0` — everything examined is intact, `1` — corruption found, `2` — some paths could not be examined.

### Reporting unknown corruption patterns

When `scan` meets damage that matches no known pattern (`other_corrupt`, or a `not_a_zip` that is not an encrypted/legacy Office file), running it with `--report` writes one `diagnostics/<id>.diag.json` fingerprint per affected file (at most 20 per run). A fingerprint contains **structural information only** — byte offsets, an entropy profile, ZIP statistics and standardized OOXML part names — never your document's text, images, file name or path. The file's basename is included only if you opt in with `--include-filenames`.

If you hit an unknown pattern, please review the fingerprint file yourself and consider attaching it to a [new issue](https://github.com/xhighhongo41/PPTrepair/issues/new/choose) using the *Unknown corruption pattern report* template. These reports are what future repair strategies get built from.

## Changelog

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
