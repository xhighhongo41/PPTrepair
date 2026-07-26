// v2.1: Web版UI。D&D受付・検証、修復キュー(逐次)、worker通信、
// 結果表示とダウンロード、i18n適用を担当する。
// セキュリティ方針: ファイル名・レポート等の動的文字列のDOM挿入は
// textContent/createElementのみ(innerHTMLへの動的代入禁止)。

const SUPPORTED_LANGS = ["en", "ja", "zh", "ko", "es", "fr", "de"];
const LANG_LABELS = {
  en: "English", ja: "日本語", zh: "中文", ko: "한국어",
  es: "Español", fr: "Français", de: "Deutsch",
};
const MAX_FILE_MB = 200; // PoC実測(2GiB制約に対しピーク約1.1GB)に基づく上限
const LANG_KEY = "pptrepair-lang";

const el = (id) => document.getElementById(id);

let strings = {}; // 現在言語のUI文字列(workerがgettextカタログから供給)
let lang = detectLang();
let worker = null;
let ready = false;
let busy = false;
let seq = 0;
const queue = []; // 未処理の {id, file}
const rows = new Map(); // id -> {tr, state}

function detectLang() {
  const saved = localStorage.getItem(LANG_KEY);
  if (saved && SUPPORTED_LANGS.includes(saved)) return saved;
  const nav = (navigator.language || "en").slice(0, 2).toLowerCase();
  return SUPPORTED_LANGS.includes(nav) ? nav : "en";
}

function t(key) {
  return strings[key] || key;
}

function applyStrings() {
  document.documentElement.lang = lang;
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  for (const { tr, state } of rows.values()) renderRow(tr, state);
}

function humanSize(bytes) {
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
  return Math.max(1, Math.round(bytes / 1024)) + " KB";
}

// 結果行の状態: {name, size, statusKey, detail?, report?, artifact?}
function renderRow(tr, state) {
  tr.replaceChildren();
  const name = document.createElement("td");
  name.textContent = state.name;
  const size = document.createElement("td");
  size.textContent = humanSize(state.size);
  const status = document.createElement("td");
  let statusText = t(state.statusKey);
  if (state.statusKey === "err_too_large") {
    statusText = statusText.replace("{limit}", String(MAX_FILE_MB));
  }
  status.textContent = statusText;
  status.className = state.statusClass || "";
  const result = document.createElement("td");
  if (state.artifact) {
    const a = document.createElement("a");
    a.href = state.artifact.url;
    a.download = state.artifact.name;
    a.textContent = t("download");
    a.className = "download";
    result.appendChild(a);
  }
  if (state.report) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = t("show_report");
    const pre = document.createElement("pre");
    pre.textContent = state.report;
    details.append(summary, pre);
    result.appendChild(details);
  }
  tr.append(name, size, status, result);
}

function addRow(state) {
  const id = ++seq;
  const tr = document.createElement("tr");
  rows.set(id, { tr, state });
  renderRow(tr, state);
  el("results-body").appendChild(tr);
  el("results").hidden = false;
  return id;
}

function setRowState(id, patch) {
  const entry = rows.get(id);
  if (!entry) return;
  Object.assign(entry.state, patch);
  renderRow(entry.tr, entry.state);
}

function addFiles(fileList) {
  for (const file of fileList) {
    const okExt = /\.(pptx|pptm)$/i.test(file.name);
    if (!okExt) {
      addRow({ name: file.name, size: file.size,
               statusKey: "err_extension", statusClass: "bad" });
      continue;
    }
    if (file.size > MAX_FILE_MB * 1048576) {
      addRow({ name: file.name, size: file.size,
               statusKey: "err_too_large", statusClass: "bad" });
      continue;
    }
    const id = addRow({ name: file.name, size: file.size,
                        statusKey: "status_queued" });
    queue.push({ id, file });
  }
  pump();
}

async function pump() {
  if (!ready || busy || queue.length === 0) return;
  const { id, file } = queue.shift();
  busy = true;
  setRowState(id, { statusKey: "status_processing" });
  try {
    const buffer = await file.arrayBuffer();
    worker.postMessage(
      { type: "repair", id, name: file.name, lang, buffer }, [buffer]
    );
  } catch (err) {
    busy = false;
    setRowState(id, { statusKey: "status_error", statusClass: "bad",
                      report: String(err) });
    pump();
  }
}

function statusFor(res) {
  if (!res.ok) return ["status_error", "bad"];
  if (!res.success) return ["status_unrepairable", "bad"];
  if (res.mode === "none") return ["status_intact", "good"];
  if (res.mode === "extract") return ["status_salvaged", "warn"];
  return ["status_repaired", "good"]; // rebuild / trim
}

function onResult(res) {
  busy = false;
  const patch = {};
  const [key, cls] = statusFor(res);
  patch.statusKey = key;
  patch.statusClass = cls;
  if (res.report) patch.report = res.report;
  if (res.error) patch.report = res.error;
  if (res.artifactBuffer && res.artifactName) {
    const blob = new Blob([res.artifactBuffer],
                          { type: "application/octet-stream" });
    patch.artifact = {
      url: URL.createObjectURL(blob),
      name: res.artifactName,
    };
  }
  setRowState(res.id, patch);
  pump();
}

function onWorkerMessage(e) {
  const msg = e.data;
  if (msg.type === "progress") {
    el("loading-text").textContent =
      msg.phase === "package" ? t("loading_package") : t("loading_engine");
  } else if (msg.type === "ready") {
    strings = msg.strings;
    ready = true;
    el("app-version").textContent = msg.version;
    el("loading").hidden = true;
    el("dropzone").hidden = false;
    el("ready-note").hidden = false;
    applyStrings();
    pump();
  } else if (msg.type === "strings") {
    if (msg.lang === lang) {
      strings = msg.strings;
      applyStrings();
    }
  } else if (msg.type === "result") {
    onResult(msg);
  } else if (msg.type === "error") {
    if (msg.id != null) {
      busy = false;
      setRowState(msg.id, { statusKey: "status_error", statusClass: "bad",
                            report: msg.error });
      pump();
    } else {
      showFatal(msg.error);
    }
  }
}

function showFatal(detail) {
  ready = false;
  el("loading").hidden = false;
  el("dropzone").hidden = true;
  el("loading-text").textContent =
    t("worker_error") + (detail ? " [" + detail + "]" : "");
}

function initLangSelect() {
  const select = el("lang-select");
  for (const code of SUPPORTED_LANGS) {
    const opt = document.createElement("option");
    opt.value = code;
    opt.textContent = LANG_LABELS[code];
    if (code === lang) opt.selected = true;
    select.appendChild(opt);
  }
  select.addEventListener("change", () => {
    lang = select.value;
    localStorage.setItem(LANG_KEY, lang);
    if (worker) worker.postMessage({ type: "strings", lang });
  });
}

function initDropzone() {
  const zone = el("dropzone");
  const input = el("file-input");
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") input.click();
  });
  input.addEventListener("change", () => {
    addFiles(input.files);
    input.value = "";
  });
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("drag");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag");
    addFiles(e.dataTransfer.files);
  });
}

function main() {
  initLangSelect();
  initDropzone();
  worker = new Worker("worker.js", { type: "module" });
  worker.onmessage = onWorkerMessage;
  worker.onerror = (e) => showFatal(e.message || "worker error");
  worker.postMessage({ type: "init", lang });
}

main();
