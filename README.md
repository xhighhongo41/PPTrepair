# PPTrepair

Repairs PowerPoint files that were corrupted while stored on OneDrive.

日本語版は [README_ja.md](README_ja.md) を参照してください。 (For the Japanese version, see [README_ja.md](README_ja.md).)

> **Note**: This project is under active development. No repair program is available yet.

## Changelog

### ver 0.1 (2026-07-12)
- Researched publicly reported cases of PowerPoint (.pptx) file corruption on OneDrive, and analyzed the binary structure of actual corrupted files
- Confirmed that the corruption is not random bit rot, but chunk-wise overwriting/truncation aligned to 256 KiB / 1 MiB boundaries, and that it falls into several distinct patterns (leading zero-fill, leading foreign data, old-version mixture, and tail truncation)
- Verified that for some patterns most slides can be salvaged by scanning the data remaining in the file, and formulated the implementation strategy for the repair tool
- Decided on Python (standard library only) as the implementation language
