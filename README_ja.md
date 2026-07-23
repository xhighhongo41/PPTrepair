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
| `interior_damage` | 中間のエントリが破損、先頭とZIPインデックスは生存 |
| `tail_foreign_data` | 完全なアーカイブの後ろに異物データが付加され、ZIPインデックスが隠された状態 |
| `full_zero_fill` | ファイルの（ほぼ）全体がゼロで上書きされ、内容が残っていない |
| `empty_file` | 0バイトの空ファイル。内容が残っていない |
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

構造上無傷のファイルに対しては、`check` はさらに3種の内容整合検査を行い、初回起動時にPowerPointが修復を提案する原因となる不整合を報告します: 未解決のXML関係参照、存在しない図形やメディアを失った図形を指したままのアニメーション（timing）参照、必須構造関係の欠落（テーマを持たないスライドマスター等）。判定と終了コードは変わりません（`--json` では `xml_ref_integrity`・`timing_integrity`・`structure_integrity` に詳細が入ります）。PPTrepair 1.1.1以前の修復で残ることのあった不整合もこれで検出できます。

### 修復（repair）

```console
$ pptrepair repair broken.pptx                 # 自動で最適な修復戦略を選択
$ pptrepair repair broken.pptx -o fixed.pptx   # 出力先を明示指定
$ pptrepair repair broken.pptx --lang ja       # レポートを日本語で出力
```

`repair` は入力ファイルを一切変更しません。破損の状態に応じて次のいずれかを生成します:

* **再構築されたプレゼンテーション** `<名前>.repaired.pptx` — 生存データにスライドが残っている場合（末尾切断型・バージョン混在型・中間破損型）。生存していた全スライドが復元されます。破損とともに画像や動画が失われた場合は、それらへの宙吊り参照もスライドXMLから自動的に除去されるため（動画の実体が失われた場合はポスターフレームが静止画として残ります）、PowerPointは修復を提案せずそのまま開けます。
* **切り出されたプレゼンテーション** `<名前>.repaired.pptx` — 完全なアーカイブの後ろに異物データが付加されている場合（`tail_foreign_data`）。付加部分を切り落として元のアーカイブをバイト単位で完全に復元します（先頭アーカイブ自体も破損している場合は再構築にフォールバック）。
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

* **クラウド専用ファイルを既定ではダウンロードしません。** 実体がクラウドにしかないプレースホルダファイル（OneDriveのファイルオンデマンド、iCloud Drive など、OS標準のプレースホルダ機構を使うもの）はメタデータだけで検出してスキップし、何件が未検査だったかを必ずサマリに表示します。この件数に数えられるのはPowerPointファイルのみです — それ以外のファイルは先にファイル名で除外されるため、クラウド専用の写真や文書がいくらあっても件数は増えません。`--allow-download` を付ければダウンロードして検査します。各ファイルのダウンロード開始時にはその旨が stderr に表示されるので、時間のかかるスキャンでも進行が見えます（時間とディスク容量を消費する場合があります）。独自実装のクライアント（特に Windows 版 Google Drive for desktop）は検出できないため、読み取り時にダウンロードが発生する可能性があります。
* 暗号化された、またはレガシーバイナリ形式のOfficeファイル（OLE複合文書）は「破損」ではなくその旨が報告されます。レガシー `.ppt` と Office の `~$` ロックファイルは件数集計してスキップ。シンボリックリンクは `--follow-symlinks` を付けない限り辿りません。
* `--report DIR` で `scan_report.txt`・機械可読な `scan_report.json`・未知破損パターン用の匿名診断フィンガープリント（後述）を書き出します。既存のレポートディレクトリには `--force` が必要です。`--lang` と `--json` は `repair` と同様に使えます。
* 終了コード: `0` — 検査対象すべて無傷、`1` — 破損あり、`2` — 検査できないパスあり。

### フォルダツリーの一括修復（repair-all）

```console
$ pptrepair repair-all ~/OneDrive/Documents -o ~/repaired    # 集約フォルダへ出力
$ pptrepair repair-all ~/slides --in-place                   # 各ファイルの隣に出力
$ pptrepair repair-all ~/slides -o ~/repaired --dry-run      # 計画表示のみ・何も書かない
```

`repair-all` は `scan` と `repair` の組み合わせです。指定ディレクトリを再帰的に走査し（クラウドプレースホルダの安全策は `scan` と同一）、見つかった破損ファイルをすべて修復します。1ファイルごとに進行が表示され、最後に修復サマリが出ます。

* **集約出力（`-o OUTDIR`）** は入力ツリーの構造を `OUTDIR` 配下にミラーして成果物を置くため、走査対象のフォルダには一切書き込みません — 同期中のOneDriveツリーに対する安全な選択です。複数のDIRを指定した場合はルートごとに `OUTDIR` 配下のサブディレクトリ（ルート名。重複時は連番付き）に分かれます。
* **`--in-place`** は単体の `pptrepair repair` と同様に、各入力ファイルの隣に成果物を置きます。同期フォルダ内では成果物も同期されること、再実行時には成果物も通常の（無傷の）ファイルとして走査対象になることに注意してください。
* 成果物の形式は単体修復と同じです: 再構築/切り出しの `<名前>.repaired.pptx`、または復元フォルダ `<名前>.salvaged/`（`REPORT.txt` 付き）。既存の成果物があるファイルはスキップされるため、中断したバッチはそのまま再実行できます（`--force` で上書き）。1ファイルの失敗が残りの処理を止めることはありません。
* 暗号化・レガシー形式のOfficeファイルは報告のみで修復は試みません。内容が残っていないファイル（空ファイル・全域ゼロ充填）は「修復不能」として正直に報告されます。
* `--dry-run` は全ファイルを診断して修復計画を表示するだけで、一切何も書き込みません（レポートも書きません）。
* `--report DIR` で `scan_report.txt`/`.json`・`repair_report.txt`/`.json`・未知パターン用の匿名診断フィンガープリントを書き出します（`scan --report` と同様）。
* 終了コード: `0` — 破損なし、または検出された破損をすべて修復済み、`1` — 未修復の破損ファイルが残った（修復不能、または既存成果物によりスキップ）、`2` — 検査できないパスがあった、または修復がエラーで失敗。

### 修復不能ファイルからの部品レスキュー（salvage）

```console
$ pptrepair salvage hopeless.pptx              # 生き残っている内容を救出
$ pptrepair salvage hopeless.pptx -o rescued   # 出力フォルダを明示
```

`repair`でも歯が立たないほど壊れたファイル（例: 先頭の巨大なブロックがその場で上書きされたもの）から、まだ生き残っている内容を取り出します。入力ファイルは一切変更しません。出力は`<名前>.rescued/`フォルダで、内容は次のとおりです:

* `entries/` — 無傷のまま読み出せた全パッケージパート（保存されたチェックサムで検証済み）
* `carved/` — 生のバイト列からシグネチャ走査で回収したJPEG/PNG画像。ZIP構造が完全に失われていても機能し、ファイルを上書きした異物データの中に埋まっていた画像さえ取り出せます（このためレポートではカービング産画像の出所を「不明」と明記します）
* `partial_xml/` — 破損XMLパートの読める先頭部分（壊れた箇所の直前まで展開）
* `rescued_text.txt` — 生存スライドXMLと部分復号XMLから回収したテキスト
* `salvage_report.json` — 上記すべての機械可読な目録

`--lang`・`--json`・`--force`は`repair`と同様です。終了コード: `0` — 何かを救出できた（または元々無傷）、`1` — 何も救出できなかった、`2` — 使用法・I/Oエラー。

### 無傷の双子からの復元候補（restore candidates）

OneDrive型のチャンク破損はバイトをその場で上書きするためファイルサイズが保存されます。そのため、スキャンツリー内の別の場所にある同じプレゼンの無傷コピー（置き忘れたコピー・古い書き出し・同期競合の兄弟ファイル）が、最速の完全復元手段になることがよくあります。`scan --report`のレポートでは、各破損ファイルの下に、同名・同サイズ（またはいずれか）の無傷ファイルを列挙します:

```
  - Projects/deck1.pptx: head_foreign_data
  復元候補: Archive/deck1.pptx（同名・同サイズ）
```

`scan_report.json`には同じ情報が`twin_candidates`（候補ごとに`high`/`medium`/`low`の信頼度付き）として入ります。`repair-all`のレポートも修復できなかったファイルについて候補を列挙し、`repair_report.json`の`schema_version`は2になりました。PPTrepairは候補を指し示すだけです——手動で置き換える前に、必ず内容を確認してください。

### 複数の破損コピーを1つに統合修復する（merge）

```console
$ pptrepair merge 本体.pptx 競合コピー.pptx               # 2つ以上のコピー
$ pptrepair merge 本体.pptx コピー2.pptx 旧バージョン.pptx --yes
```

OneDrive破損は、同じプレゼンの「壊れ方の違う」コピーを複数残すことがよくあります（作業ファイル+同期競合コピーなど）。`merge`はそれらすべてから1つのファイルを再構成します。第1引数が復元対象、以降の引数が部品の供給元です。各アーカイブエントリは「ファイル自身の索引に記録されたCRC-32チェックサムを再現できるコピー」から採用されるため、誤ったバイトが黙って混入することはありません。どのコピーにも丸ごと残っていないエントリは、この破損の動作単位である64KiB境界で断片を組み合わせて復元を試みます。

供給元は使用前に審査されます。サイズ完全一致で索引もよく一致するコピーは自動で使用。一致の弱い同サイズコピーや、同じプレゼンの*別バージョン*（主に埋め込みメディアのバイト一致で認識）は、根拠を提示する対話確認を経てのみ使用されます（スクリプト用に`--allow-candidate`/`--yes`でスキップ可能）。

結果には保証レベルが付きます:

* `full` — 全エントリ検証済み。出力は**元ファイルとバイト単位で同一**
* `partial` — 検証できたものだけを復元・再パッケージ。未検証の内容は含まない
* `hybrid` — 一部を別バージョン由来の内容で補完。失われた原本と同一である保証は*ない*（警告で明示されます）
* `failed` — 再構成不能（exit code 1）

`scan --report`と`repair-all`のレポートはマージの機会も指摘します: バイトサイズが完全一致する破損ファイル群は実行可能なコマンド例付きの「マージ候補グループ」として、別バージョンらしきファイルは類似スコア付きの「別バージョン候補」（`lineage_candidates`）として列挙されます。`repair_report.json`の`schema_version`は3になりました。

### 未知の破損パターンの報告

既知パターンに一致しない破損（`other_corrupt`、または暗号化/レガシーOfficeでない `not_a_zip`）に遭遇した場合、`--report` 付きで実行すると該当ファイルごとに `diagnostics/<id>.diag.json`（1回の実行で最大20件）が書き出されます。フィンガープリントに含まれるのは**構造情報のみ**です — バイトオフセット、エントロピープロファイル、ZIP統計、標準化されたOOXMLパート名。文書のテキスト・画像・ファイル名・パスは一切含まれません（ファイル名は `--include-filenames` を明示した場合のみbasenameが入ります）。

未知パターンに遭遇したら、フィンガープリントの中身をご自身で確認のうえ、[新しいissue](https://github.com/xhighhongo41/PPTrepair/issues/new/choose) に *Unknown corruption pattern report* テンプレートで添付いただけると助かります。この報告が将来の修復ロジックの開発材料になります。

## Changelog

### ver 1.3 (2026-07-23)
- 新コマンド`pptrepair merge`を追加: 壊れ方の違う同一プレゼンの複数コピー（本数任意）から1つの復元ファイルを再構成します。採用する全バイト範囲をファイル自身の索引に記録されたCRC-32で検証し（`full`の出力は原本とバイト同一——実物の無傷双子との一致で実証済み）、どのコピーにも丸ごと残らないエントリは64KiB破損境界で断片を組み合わせ、同じプレゼンの*別バージョン*も対話確認を経て部品供給に使えます（`hybrid`の出力は別バージョン由来の箇所を明示します）。ドナー部品の照合は部品名ではなく内容ベースなので、版間のスライド挿入で番号がずれたスライドやメディアも発見され、正しい位置に復元されます
- `scan --report` / `repair-all`のレポートにマージ候補グループ（同サイズ破損ファイル群+実行可能な`merge`コマンド例）と別バージョン候補（共有メディアでスコア付け）を追加。`repair_report.json`の`schema_version`は3になりました
- パッケージバージョンの定義を`pptrepair.__version__`の1箇所に一元化（setuptools動的メタデータ）

### ver 1.2.1.1 (2026-07-22)
- `pptrepair --version`（および診断フィンガープリントの`tool_version`）が1.2.0のまま表示される問題を修正: 1.2.1リリースでパッケージ内バージョン文字列の更新が漏れていました。機能的な変更はありません

### ver 1.2.1 (2026-07-22)
- 新コマンド`pptrepair salvage`を追加: 修復不能なほど壊れたファイルから、無傷のパッケージパート・生バイト列からカービングしたJPEG/PNG画像（ファイルを上書きした異物データの中の画像も対象）・破損XMLの読める先頭部分・ベストエフォートのテキスト・機械可読な`salvage_report.json`を救出します
- `scan --report`と`repair-all`のレポートに**復元候補**を追加: スキャンツリー内で破損ファイルと同名・同サイズ（またはいずれか）の無傷ファイルを列挙します（この種の破損はファイルサイズを保存するため、こうした双子はしばしば完全復元手段になります）。`repair_report.json`の`schema_version`は2になりました
- 先頭大領域上書きの分類を改善: 実データの調査で、上書きしてきたデータ自体が*別の*ZIPアーカイブのCRC有効な断片を含み得ることが判明し、これが`head_foreign_data`検出器を欺いて`other_corrupt`に誤分類させていました。検出器は走査エントリをCentral Directoryと突合するようになり、異物断片の名前を証拠として表示します。また先頭に限局しない類縁の破損を分類する2つのフォールバック判定（`foreign_zip_overwrite`・`scattered_overwrite`）を追加しました

### ver 1.2 (2026-07-19)
- `pptrepair repair-all` コマンドを追加: 1つ以上のディレクトリツリーを再帰走査し、見つかった破損 `.pptx` / `.pptm` をすべて修復。出力は入力ツリーをミラーする集約フォルダ（`-o OUTDIR`。走査対象フォルダには一切書き込まない）または各ファイルの隣（`--in-place`）を選択可能。1ファイルごとの進行表示と修復サマリ付き、クラウドプレースホルダの安全策は `scan` と同一
- `--dry-run` で一切書き込まずに修復計画のみを表示。`--report DIR` で `scan_report.txt`/`.json`・`repair_report.txt`/`.json`・未知パターン用の匿名診断フィンガープリントを保存
- バッチは再実行可能: 既存の成果物は `--force` なしではスキップされ、1ファイルの失敗が残りを止めることはない。暗号化・レガシーOfficeファイルは報告のみで修復は試みない

### ver 1.1.2 (2026-07-17)
- 再構築後のファイルが、PowerPointの修復提案なしでそのまま開けるようになりました。実データ検証で確認された3つの原因すべてに対処します: スライドXML内の宙吊り関係参照の除去（動画の実体が失われた場合はポスターフレームが静止画として残ります）、メディアを失った図形・除去された図形とアニメーション（timing）ツリーの整合、破損でテーマが失われたスライドマスターへのデフォルトOfficeテーマの合成
- `check` に3種の内容整合検査を追加: 未解決関係参照・timing参照の不整合・必須構造関係の欠落を報告します（`--json` では `xml_ref_integrity`・`timing_integrity`・`structure_integrity`）。判定・終了コードは変わりません。過去バージョンの修復生成物の不整合もこれで検出できます
- `repair` は生成した成果物を同じ3検査で自己検証し結果を報告します。trim成果物は元アーカイブとバイト単位で同一のため、元から存在した不整合は報告のみ行い変更しません

### ver 1.1.1 (2026-07-17)
- 最初に収集された診断フィンガープリントから特定した破損ジオメトリに対応する4つの判定を追加: `interior_damage`・`tail_foreign_data`・`full_zero_fill`・`empty_file`
- `repair` にtrim戦略を追加: `tail_foreign_data` は付加された異物データを切り落として元のアーカイブをバイト単位で完全復元（先頭アーカイブ自体が破損している場合はsalvageベースの再構築にフォールバック）。`interior_damage` は既存の再構築経路で修復
- 先頭破損の検出を高感度化: `head_zero_fill` の先頭ゼロ域閾値を64KiBから4KiBに引き下げ、`head_foreign_data` は異物データがゼロバイトで始まる場合も認識
- 空ファイル・全域ゼロ充填ファイルが診断フィンガープリントの枠を消費しないようにし、「内容が残っていない」ことを正直に案内（未知パターンとしての報告誘導をしない）
- 既知の制限: 画像が失われたスライドを復元した場合、再構築後のファイルには失われた画像への参照が残るため、初回起動時にPowerPointが一度だけ修復を提案することがあります（残存参照の自動除去は次リリースで対応予定）

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
