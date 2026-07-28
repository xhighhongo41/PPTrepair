// v2.1: 修復エンジンworker。
// Pyodide(セルフホスト・バージョン固定)をロードし、pptrepair wheelを
// unpackArchiveで導入して、メインスレッドからの修復要求を逐次処理する。
// ファイル内容はこのworker(=利用者のブラウザ内)から外に出ない。
import { loadPyodide } from "./vendor/pyodide/pyodide.mjs";

// Python側ブリッジ。web_repair()は1ファイル修復の結果をJSONで返し、
// 成果物(修復zip / サルベージzip)は/work内に置く。extractモードの
// ディレクトリ出力はダウンロード可能なようにzip化する(PoC記録§3)。
const BRIDGE = `
import json
import os
import shutil
import zipfile
from pathlib import Path

from pptrepair.i18n import get_translator
from pptrepair.repair import repair_file
from pptrepair.report_repair import render_repair_text
from pptrepair.webstrings import ui_strings

WORK = Path("/work")


def web_reset():
    """前回の入力・成果物を破棄して/workを空にする。"""
    os.chdir("/")
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir()
    # レポートに内部パス(/work/)が出ないよう相対パスで処理する
    os.chdir(WORK)


def web_strings(lang):
    return json.dumps(ui_strings(lang))


def web_repair(src_name, lang):
    """/work/<src_name> を修復し、結果メタデータをJSONで返す。"""
    src = Path(src_name)
    tr = get_translator(lang)
    try:
        outcome = repair_file(src, lang=lang)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        })
    report = render_repair_text(outcome, tr)
    artifact_path = None
    artifact_name = None
    if outcome.output_path is not None:
        out = Path(outcome.output_path)
        if out.is_dir():
            zpath = WORK / (src.stem + ".salvaged.zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(out.rglob("*")):
                    if p.is_file():
                        zf.write(p, str(p.relative_to(out)))
            artifact_path = str(zpath)
            artifact_name = zpath.name
        else:
            # cwd=/workの相対パスで来るため、JS側FS.readFile用に絶対化する
            artifact_path = str(out.resolve())
            artifact_name = out.name
    return json.dumps({
        "ok": True,
        "mode": outcome.mode,
        "success": outcome.success,
        "verdict": outcome.diagnosis.verdict.value,
        "recheckVerdict": outcome.recheck_verdict,
        "report": report,
        "artifactPath": artifact_path,
        "artifactName": artifact_name,
    })
`;

let pyodide = null;
let webRepair = null;
let webStrings = null;
let webReset = null;

async function init(lang) {
  postMessage({ type: "progress", phase: "engine" });
  pyodide = await loadPyodide({ indexURL: "./vendor/pyodide/" });

  postMessage({ type: "progress", phase: "package" });
  const manifest = await (await fetch("./wheels/manifest.json")).json();
  const wheelBuf = await (
    await fetch(`./wheels/${manifest.wheel}`)
  ).arrayBuffer();
  pyodide.unpackArchive(wheelBuf, "wheel");

  pyodide.runPython(BRIDGE);
  webRepair = pyodide.globals.get("web_repair");
  webStrings = pyodide.globals.get("web_strings");
  webReset = pyodide.globals.get("web_reset");

  postMessage({
    type: "ready",
    version: manifest.version,
    strings: JSON.parse(webStrings(lang)),
    lang,
  });
}

function repair({ id, name, lang, buffer }) {
  // パス区切りを潰したファイル名で/workに書き込む(トラバーサル防止)
  const safe = name.replace(/[/\\]/g, "_") || "presentation.pptx";
  webReset();
  pyodide.FS.writeFile(`/work/${safe}`, new Uint8Array(buffer));
  const res = JSON.parse(webRepair(safe, lang));
  let artifactBuffer = null;
  if (res.ok && res.artifactPath) {
    // FS.readFileは独立したUint8Arrayを返すのでtransferして良い
    artifactBuffer = pyodide.FS.readFile(res.artifactPath).buffer;
  }
  webReset();
  postMessage(
    { type: "result", id, ...res, artifactBuffer },
    artifactBuffer ? [artifactBuffer] : []
  );
}

onmessage = async (e) => {
  const msg = e.data;
  try {
    if (msg.type === "init") {
      await init(msg.lang);
    } else if (msg.type === "strings") {
      postMessage({
        type: "strings",
        lang: msg.lang,
        strings: JSON.parse(webStrings(msg.lang)),
      });
    } else if (msg.type === "repair") {
      repair(msg);
    }
  } catch (err) {
    postMessage({
      type: "error",
      id: msg.id ?? null,
      error: String((err && err.message) || err),
    });
  }
};
