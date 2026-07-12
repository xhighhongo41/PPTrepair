# PPTrepair

Diagnoses (and will eventually repair) PowerPoint files that were corrupted while stored on OneDrive.

日本語版は [README_ja.md](README_ja.md) を参照してください。 (For the Japanese version, see [README_ja.md](README_ja.md).)

> **Note**: This project is under active development. Version 0.2 provides diagnosis only; repair is planned for version 1.0.

## What it does

`pptrepair check` inspects `.pptx` files and reports whether each one is an intact PowerPoint package or matches one of the OneDrive corruption patterns identified by this project. Real-world analysis showed that these corruptions are chunk-wise overwrites or truncations aligned to 256 KiB / 1 MiB boundaries, falling into distinct patterns:

| Verdict | Meaning |
|---|---|
| `normal` | intact PowerPoint package (every entry readable) |
| `head_zero_fill` | leading chunks overwritten with zeros; only the file tail survives |
| `head_foreign_data` | leading chunks overwritten with unrelated data |
| `version_mix` | a collage of chunks from two different save versions |
| `tail_truncated` | file cut off prematurely; the ZIP central directory is lost |
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

## Changelog

### ver 0.2 (2026-07-12)
- Added the `pptrepair check` command, which classifies `.pptx` files as intact or as one of the known OneDrive corruption patterns, with evidence and a salvageability summary
- Text and `--json` output, scripting-friendly exit codes
- Installable Python package (`pip install .`), standard library only

### ver 0.1 (2026-07-12)
- Researched publicly reported cases of PowerPoint (.pptx) file corruption on OneDrive, and analyzed the binary structure of actual corrupted files
- Confirmed that the corruption is not random bit rot, but chunk-wise overwriting/truncation aligned to 256 KiB / 1 MiB boundaries, and that it falls into several distinct patterns (leading zero-fill, leading foreign data, old-version mixture, and tail truncation)
- Verified that for some patterns most slides can be salvaged by scanning the data remaining in the file, and formulated the implementation strategy for the repair tool
- Decided on Python (standard library only) as the implementation language
