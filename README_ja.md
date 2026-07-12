# PPTrepair

OneDriveに置いている間に破損したPowerPointファイルを診断する（将来的には修復する）ツール。

English version: [README.md](README.md)

> **注**: 現在開発中です。バージョン0.2では診断機能のみを提供し、修復機能はバージョン1.0で提供予定です。

## このツールができること

`pptrepair check` は `.pptx` ファイルを検査し、各ファイルが「無傷のPowerPointパッケージ」か「本プロジェクトが特定したOneDrive破損パターンのいずれか」かを判定します。実ファイルの分析により、この破損は256KiB/1MiB境界に整列したチャンク単位の上書き・切断であり、以下のパターンに分類できることがわかっています:

| 判定 | 意味 |
|---|---|
| `normal` | 無傷のPowerPointパッケージ（全エントリ読取可能） |
| `head_zero_fill` | 先頭チャンク群がゼロで上書きされ、末尾のみ生存 |
| `head_foreign_data` | 先頭チャンク群が無関係なデータで上書き |
| `version_mix` | 2つの保存バージョンのチャンクが混在 |
| `tail_truncated` | ファイル末尾が切断され、ZIPセントラルディレクトリを喪失 |
| `other_corrupt` | 破損しているが既知パターンに一致しない |
| `not_a_zip` | ZIP形式のファイルではない |

破損ファイルについては、どれだけの内容（エントリ数・スライド数）が救出可能かも表示します。これは将来の修復機能で何が復元できるかの目安になります。すべてのファイルは読み取り専用で扱われ、データが変更されることはありません。

## インストール

Python 3.12以降が必要です。標準ライブラリ以外の実行時依存はありません。お使いのPython環境管理ツールに合わせて以下のいずれかの方法を選んでください。

### pipx（CLIツールにおすすめ）

`pptrepair` を独立した環境にインストールし、どこからでも使えるコマンドにします:

```console
$ pipx install git+https://github.com/xhighhongo41/PPTrepair.git
$ pptrepair check presentation.pptx
```

クローン済みのディレクトリ内なら `pipx install .` でも同様です。

### uv

```console
$ uv tool install git+https://github.com/xhighhongo41/PPTrepair.git
$ pptrepair check presentation.pptx
```

クローン内であればインストールせずに直接実行することもできます:

```console
$ uv run pptrepair check presentation.pptx
```

### pip + venv（Python標準ツール）

```console
$ git clone https://github.com/xhighhongo41/PPTrepair.git
$ cd PPTrepair
$ python -m venv .venv
$ source .venv/bin/activate    # Windowsの場合: .venv\Scripts\activate
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

### pyenv / conda をお使いの場合

pyenvはPythonのバージョン管理ツールなので、`python` が3.12以降を指すようにした上で（例: `pyenv install 3.13 && pyenv shell 3.13`）、上記いずれかの方法を使ってください。condaの場合は環境を作ってからpipでインストールします: `conda create -n pptrepair python=3.13 && conda activate pptrepair && pip install .`

## 使い方

```console
$ pptrepair check presentation.pptx [more.pptx ...]
$ pptrepair check --json presentation.pptx   # 機械可読なJSON出力
```

末尾切断ファイルの出力例:

```
=== presentation.pptx ===
Verdict: tail_truncated (corrupted: file tail truncated)
Evidence:
  - no end-of-central-directory record found
  - file starts with a local file header signature
  - 192 local file header(s) found
Salvageable: 190/192 entries, 37/37 slides
```

終了コード: `0` — 全ファイル無傷、`1` — 1つ以上のファイルが破損、`2` — 引数・入出力エラー。

## Changelog

### ver 0.2 (2026-07-12)
- `.pptx` ファイルを「無傷」または既知のOneDrive破損パターンに分類する `pptrepair check` コマンドを追加（判定根拠と救出可能性サマリ付き）
- テキスト出力と `--json` 出力、スクリプトから使いやすい終了コード
- Pythonパッケージとしてインストール可能（`pip install .`）、標準ライブラリのみ使用

### ver 0.1 (2026-07-12)
- OneDrive上で発生するPowerPoint (.pptx) ファイル破損について、公開事例のWeb調査と実際の破損ファイルのバイナリ構造調査を実施
- 破損はランダムなデータ化けではなく「チャンク単位（256KiB/1MiB境界に整列）の上書き・切断」であり、複数のパターン（先頭ゼロ埋め型・先頭異物データ型・旧バージョン混在型・末尾切断型）に分類できることを確認
- 一部のパターンでは、ファイル内に残存するデータの走査により大部分のスライドを救出できることを検証し、修復ツールの実装方針を策定
- 開発言語をPython（標準ライブラリのみ使用）に決定
