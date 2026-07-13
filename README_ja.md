# PPTrepair

OneDriveに置いている間に破損したPowerPointファイルを診断・修復するツール。

English version: [README.md](README.md)

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

### 修復（repair）

```console
$ pptrepair repair broken.pptx                 # 自動で最適な修復戦略を選択
$ pptrepair repair broken.pptx -o fixed.pptx   # 出力先を明示指定
$ pptrepair repair broken.pptx --lang ja       # レポートを日本語で出力
```

`repair` は入力ファイルを一切変更しません。破損の状態に応じて次のいずれかを生成します:

* **再構築されたプレゼンテーション** `<名前>.repaired.pptx` — 生存データにスライドが残っている場合（末尾切断型・バージョン混在型）。本プロジェクトの実破損ファイル群では、生存していた全スライドが復元され、PowerPointで開けることを確認しています。
* **復元フォルダ** `<名前>.salvaged/` — スライド本体が失われている場合。生存した画像は `images/`、音声・動画は `media/`、復元できたテキスト（スライドタイトル一覧・文書情報）は `texts/`、グラフのデータは `charts/`、全パートの生データは `parts/` に整理され、何が失われたかを明記した `REPORT.txt` が付きます。

レポートの言語は `--lang` で選択できます（`en`, `ja`, `zh`, `ko`, `es`, `fr`, `de`。既定は英語）。既存の出力先は `--force` なしでは上書きしません。`--json` で機械可読な結果も得られます。終了コード: `0` — 成果物を生成（または元々無傷）、`1` — 復元可能な内容なし、`2` — 引数・入出力エラー。

### フォルダツリーの一括スキャン（scan）

```console
$ pptrepair scan ~/OneDrive/Documents            # 破損ファイルを探す
$ pptrepair scan ~/slides --report scan_out      # レポートも保存
```

`scan` は指定ディレクトリを再帰的に走査し、見つかった `.pptx` / `.pptm` をすべて診断します（読み取り専用。`--report` なしでは何も書き込みません）。破損ファイルは見つかり次第表示され、最後に集計サマリが出ます:

```
Projects/deck1.pptx: head_zero_fill
Projects/old/deck2.pptx: tail_truncated
=== スキャン概要 ===
スキャン件数: 16 件
  normal: 14
  head_zero_fill: 1
  tail_truncated: 1
```

知っておくと便利な点:

* **クラウド専用ファイルを既定ではダウンロードしません。** 実体がクラウドにしかないプレースホルダファイル（OneDriveのファイルオンデマンド、iCloud Drive など、OS標準のプレースホルダ機構を使うもの）はメタデータだけで検出してスキップし、何件が未検査だったかを必ずサマリに表示します。`--allow-download` を付ければダウンロードして検査します。各ファイルのダウンロード開始時にはその旨が stderr に表示されるので、時間のかかるスキャンでも進行が見えます（時間とディスク容量を消費する場合があります）。独自実装のクライアント（特に Windows 版 Google Drive for desktop）は検出できないため、読み取り時にダウンロードが発生する可能性があります。
* 暗号化された、またはレガシーバイナリ形式のOfficeファイル（OLE複合文書）は「破損」ではなくその旨が報告されます。レガシー `.ppt` と Office の `~$` ロックファイルは件数集計してスキップ。シンボリックリンクは `--follow-symlinks` を付けない限り辿りません。
* `--report DIR` で `scan_report.txt`・機械可読な `scan_report.json`・未知破損パターン用の匿名診断フィンガープリント（後述）を書き出します。既存のレポートディレクトリには `--force` が必要です。`--lang` と `--json` は `repair` と同様に使えます。
* 終了コード: `0` — 検査対象すべて無傷、`1` — 破損あり、`2` — 検査できないパスあり。

### 未知の破損パターンの報告

既知パターンに一致しない破損（`other_corrupt`、または暗号化/レガシーOfficeでない `not_a_zip`）に遭遇した場合、`--report` 付きで実行すると該当ファイルごとに `diagnostics/<id>.diag.json`（1回の実行で最大20件）が書き出されます。フィンガープリントに含まれるのは**構造情報のみ**です — バイトオフセット、エントロピープロファイル、ZIP統計、標準化されたOOXMLパート名。文書のテキスト・画像・ファイル名・パスは一切含まれません（ファイル名は `--include-filenames` を明示した場合のみbasenameが入ります）。

未知パターンに遭遇したら、フィンガープリントの中身をご自身で確認のうえ、[新しいissue](https://github.com/xhighhongo41/PPTrepair/issues/new/choose) に *Unknown corruption pattern report* テンプレートで添付いただけると助かります。この報告が将来の修復ロジックの開発材料になります。

## Changelog

### ver 1.1 (2026-07-13)
- `pptrepair scan` コマンドを追加: ディレクトリツリーを再帰走査して破損した `.pptx` / `.pptm` を全数診断。ファイルごとの判定を逐次表示し、最後に集計サマリを出力（レポート7言語対応）
- クラウド専用プレースホルダファイル（OneDriveファイルオンデマンド・iCloud Drive等のOS標準機構）をメタデータのみで検出し、ダウンロードを誘発せずスキップ。`--allow-download` でオプトイン可能。未検査件数は必ず表示
- opt-in の `--report` フォルダ: `scan_report.txt` / `scan_report.json` と、未知破損パターン用の匿名・共有可能な診断フィンガープリント（バージョン付きスキーマ・文書内容は不含有）を生成。報告用のGitHub issueテンプレートも追加
- 暗号化・レガシーバイナリ形式のOfficeファイル（OLE複合文書）を判別し、破損候補として誤報告しないように改善

### ver 1.0 (2026-07-12)
- `pptrepair repair` コマンドを追加: 末尾切断型・バージョン混在型からは整合性のある「開ける.pptx」を再構築し、スライド本体が失われている場合は復元フォルダ（画像・メディア・復元テキスト・グラフデータ・生パート・損害レポート）を生成
- `--lang` によるレポートの7言語対応（en, ja, zh, ko, es, fr, de。標準的なGNU gettextカタログで実装）
- 入力ファイルは一切変更せず、既存出力の上書きには `--force` が必要

### ver 0.2 (2026-07-12)
- `.pptx` ファイルを「無傷」または既知のOneDrive破損パターンに分類する `pptrepair check` コマンドを追加（判定根拠と救出可能性サマリ付き）
- テキスト出力と `--json` 出力、スクリプトから使いやすい終了コード
- Pythonパッケージとしてインストール可能（`pip install .`）、標準ライブラリのみ使用

### ver 0.1 (2026-07-12)
- OneDrive上で発生するPowerPoint (.pptx) ファイル破損について、公開事例のWeb調査と実際の破損ファイルのバイナリ構造調査を実施
- 破損はランダムなデータ化けではなく「チャンク単位（256KiB/1MiB境界に整列）の上書き・切断」であり、複数のパターン（先頭ゼロ埋め型・先頭異物データ型・旧バージョン混在型・末尾切断型）に分類できることを確認
- 一部のパターンでは、ファイル内に残存するデータの走査により大部分のスライドを救出できることを検証し、修復ツールの実装方針を策定
- 開発言語をPython（標準ライブラリのみ使用）に決定
