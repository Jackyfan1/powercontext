/* StepSeek 前端主逻辑：本地向量化 + seekdb 混合检索 + 会话状态 + 量化实测 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  collections: [],
  collectionId: localStorage.getItem("stepseek.collection") || "",
  sessionId: localStorage.getItem("stepseek.session") || "",
  session: null,
  device: "wasm",
  dtype: "q8",
  wantedDtype: null,
  ready: false,
  topk: 5,
  mode: "hybrid",
  weightVector: 0.7,
  modelInfo: null,
  busy: false,
  fileName: "",
};

/* ------------------------------------------------------------------ */
/* 基础工具                                                            */
/* ------------------------------------------------------------------ */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch (_) {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json();
}

function fmtBytes(bytes) {
  if (!bytes) return "-";
  const mb = bytes / 1000000;
  return mb >= 100 ? `${mb.toFixed(0)} MB` : `${mb.toFixed(1)} MB`;
}

function nowTime() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

function log(message, level = "") {
  const li = document.createElement("li");
  if (level) li.className = level;
  li.innerHTML = `<span class="t">${nowTime()}</span><span>${message}</span>`;
  const box = $("#sys-log");
  box.prepend(li);
  while (box.children.length > 60) box.lastChild.remove();
}

function importLog(message, level = "") {
  const li = document.createElement("li");
  if (level) li.className = level;
  li.innerHTML = `<span class="t">${nowTime()}</span><span>${message}</span>`;
  const box = $("#import-log");
  box.prepend(li);
  while (box.children.length > 30) box.lastChild.remove();
}

function banner(message, kind = "") {
  const el = $("#banner");
  if (!message) {
    el.classList.add("hidden");
    return;
  }
  el.textContent = message;
  el.className = `banner ${kind}`;
}

function setChip(id, text, cls = "") {
  const el = $(id);
  el.className = `chip ${cls}`;
  el.innerHTML = `<i class="dot"></i>${text}`;
}

function escapeHtml(text) {
  return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

function highlight(text, query) {
  const safe = escapeHtml(text);
  const terms = [...new Set(query.split(/[\s,，。！？!?、;；:：()（）]+/).filter((t) => t.length >= 2))];
  if (!terms.length) return safe;
  const pattern = terms.sort((a, b) => b.length - a.length).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  return safe.replace(new RegExp(pattern, "gi"), (m) => `<mark>${m}</mark>`);
}

function sha256(text) {
  const data = new TextEncoder().encode(text);
  return crypto.subtle.digest("SHA-256", data).then((buf) =>
    Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("")
  );
}

/** 按段落切块，块长不超过 maxLen，超长段落按句切，仍超长则硬切。 */
function chunkText(text, maxLen = 320) {
  const normalized = text.replace(/\r\n/g, "\n").trim();
  if (!normalized) return [];
  const paragraphs = normalized.split(/\n\s*\n+/).map((p) => p.trim()).filter(Boolean);
  const chunks = [];
  let buffer = "";

  const push = (piece) => {
    if (buffer && buffer.length + piece.length + 1 > maxLen) {
      chunks.push(buffer.trim());
      buffer = "";
    }
    buffer = buffer ? `${buffer}\n${piece}` : piece;
  };

  for (const paragraph of paragraphs) {
    if (paragraph.length <= maxLen) {
      push(paragraph);
      continue;
    }
    const sentences = paragraph.replace(/([。！？!?；;])/g, "$1\n").split("\n").filter((s) => s.trim());
    for (const sentence of sentences) {
      if (sentence.length <= maxLen) {
        push(sentence.trim());
      } else {
        for (let i = 0; i < sentence.length; i += maxLen) {
          push(sentence.slice(i, i + maxLen).trim());
        }
      }
    }
  }
  if (buffer.trim()) chunks.push(buffer.trim());
  return chunks;
}

/* ------------------------------------------------------------------ */
/* Embedding Worker 管理                                               */
/* ------------------------------------------------------------------ */

class Embedder {
  constructor() {
    this.worker = null;
    this.pending = new Map();
    this.seq = 0;
    this.onStatus = () => {};
    this.onReady = () => {};
    this.onError = () => {};
    this.readyPromise = null;
    this.readyResolve = null;
  }

  start() {
    if (this.worker) return;
    this.worker = new Worker("/embed-worker.js", { type: "module" });
    this.worker.onmessage = (event) => {
      const msg = event.data;
      if (msg.type === "embedded") {
        const item = this.pending.get(msg.id);
        if (item) {
          this.pending.delete(msg.id);
          item.resolve({ vectors: msg.vectors, ms: msg.ms });
        }
        return;
      }
      if (msg.type === "ready") {
        this.onReady(msg);
        if (this.readyResolve) {
          this.readyResolve(msg);
          this.readyResolve = null;
        }
        return;
      }
      if (msg.type === "error") {
        if (msg.id && this.pending.has(msg.id)) {
          this.pending.get(msg.id).reject(new Error(msg.message));
          this.pending.delete(msg.id);
          return;
        }
        this.onError(msg);
        if (this.readyResolve) {
          this.readyResolve(null);
          this.readyResolve = null;
        }
        return;
      }
      if (msg.type === "status") {
        this.onStatus(msg);
      }
    };
  }

  init(device, dtype) {
    this.start();
    this.readyPromise = new Promise((resolve) => {
      this.readyResolve = resolve;
      this.worker.postMessage({ type: "init", device, dtype });
    });
    return this.readyPromise;
  }

  dispose() {
    if (!this.worker) return Promise.resolve();
    return new Promise((resolve) => {
      const handler = (event) => {
        if (event.data.type === "disposed") {
          this.worker.removeEventListener("message", handler);
          resolve();
        }
      };
      this.worker.addEventListener("message", handler);
      this.worker.postMessage({ type: "dispose" });
    });
  }

  embed(texts) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage({ type: "embed", id, texts });
    });
  }
}

const embedder = new Embedder();

embedder.onStatus = (msg) => {
  if (msg.phase === "download") {
    setChip("#chip-model", `加载模型 ${msg.progress}% (${msg.file})`, "warn");
  } else if (msg.phase === "loading") {
    setChip("#chip-model", `初始化 ${msg.device.toUpperCase()} 后端…`, "warn");
  } else if (msg.phase === "fallback") {
    log(msg.message, "err");
    banner(msg.message);
  }
};

embedder.onReady = (msg) => {
  state.device = msg.device;
  state.dtype = msg.dtype;
  state.ready = true;
  const label = msg.device === "webgpu" ? "WebGPU" : "WASM";
  const dtypeLabel = msg.dtype.toUpperCase().replace("Q8", "INT8");
  setChip("#chip-device", `${label} · ${dtypeLabel}`, msg.device === "webgpu" ? "ok" : "warn");
  setChip("#chip-model", `bge-small-zh-v1.5 · ${dtypeLabel}`, "ok");
  log(`模型就绪：${label} · ${dtypeLabel} · 加载 ${msg.load_ms} ms${msg.reused ? " (复用)" : ""}`, "ok");
  if (msg.fallback) {
    setChip("#chip-device", `WASM · INT8（已降级）`, "warn");
    banner("当前浏览器不支持 WebGPU，已自动降级为 WASM(INT8) 推理，核心流程不受影响。", "warn");
  }
  enableActions(true);
};

embedder.onError = (msg) => {
  state.ready = false;
  enableActions(false);
  setChip("#chip-model", "模型加载失败", "err");
  banner(`模型加载失败：${msg.message}`, "err");
  log(`模型错误（${msg.scope}）：${msg.message}`, "err");
};

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */

async function detectWebGPU() {
  if (!("gpu" in navigator)) return false;
  try {
    const adapter = await navigator.gpu.requestAdapter();
    return !!adapter;
  } catch (_) {
    return false;
  }
}

async function init() {
  bindEvents();
  log("页面启动，检测推理后端…");

  try {
    const health = await api("/api/health");
    setChip("#chip-db", `seekdb ${health.seekdb.version.split("-").slice(-1)[0]}`, "ok");
    log(`seekdb 已连接：${health.seekdb.host}:${health.seekdb.port}/${health.seekdb.database}`, "ok");
    renderIndex(health.index);
  } catch (err) {
    setChip("#chip-db", "seekdb 未连接", "err");
    banner(`无法连接 seekdb：${err.message}。请先按 README 启动 seekdb 后刷新页面。`, "err");
    log(`seekdb 连接失败：${err.message}`, "err");
  }

  try {
    state.modelInfo = await api("/api/model/info");
  } catch (_) {
    /* ignore */
  }

  const forcedBackend = new URLSearchParams(location.search).get("backend");
  const hasWebGPU = forcedBackend === "wasm" ? false : await detectWebGPU();
  state.wantedDtype = hasWebGPU ? "fp16" : "q8";
  if (forcedBackend === "wasm") {
    log("URL 指定 backend=wasm，强制以 WASM 后端验证降级链路");
  }
  log(`WebGPU ${hasWebGPU ? "可用" : "不可用"}，使用 ${hasWebGPU ? "FP16" : "INT8(q8)"} 权重`);
  embedder.init(hasWebGPU ? "webgpu" : "wasm", state.wantedDtype);

  await loadCollections();
  await restoreSession();
  enableActions(Boolean(state.collectionId));
}

function enableActions(enabled) {
  $("#btn-search").disabled = !enabled || !state.ready;
  $("#btn-import").disabled = !enabled || !state.ready;
  $("#btn-refresh").disabled = !state.collectionId;
}

/* ------------------------------------------------------------------ */
/* 集合                                                                */
/* ------------------------------------------------------------------ */

async function loadCollections() {
  state.collections = await api("/api/collections");
  if (!state.collections.length) {
    const created = await api("/api/collections", {
      method: "POST",
      body: { name: "默认知识库", description: "自动创建，可直接导入语料" },
    });
    state.collections = [created];
    log("已自动创建默认集合", "ok");
  }
  if (!state.collections.some((c) => c.id === state.collectionId)) {
    state.collectionId = state.collections[0].id;
    localStorage.setItem("stepseek.collection", state.collectionId);
  }
  renderCollections();
  renderStats();
}

function currentCollection() {
  return state.collections.find((c) => c.id === state.collectionId) || null;
}

function renderCollections() {
  const box = $("#collection-list");
  box.innerHTML = "";
  for (const col of state.collections) {
    const item = document.createElement("div");
    item.className = `collection-item ${col.id === state.collectionId ? "active" : ""}`;
    item.innerHTML = `
      <div>
        <div class="name">${escapeHtml(col.name)}</div>
        <div class="meta">${col.doc_count} 篇 · ${col.chunk_count} 块</div>
      </div>
      <button class="del" title="删除集合">✕</button>`;
    item.addEventListener("click", async (event) => {
      if (event.target.classList.contains("del")) {
        event.stopPropagation();
        if (!confirm(`删除集合「${col.name}」及其全部数据？`)) return;
        await api(`/api/collections/${col.id}`, { method: "DELETE" });
        log(`已删除集合 ${col.name}`);
        if (state.collectionId === col.id) {
          state.collectionId = "";
          state.sessionId = "";
          localStorage.removeItem("stepseek.session");
        }
        await loadCollections();
        await restoreSession();
        return;
      }
      if (state.collectionId === col.id) return;
      state.collectionId = col.id;
      state.sessionId = "";
      localStorage.setItem("stepseek.collection", col.id);
      localStorage.removeItem("stepseek.session");
      renderCollections();
      renderStats();
      await restoreSession();
      log(`切换到集合「${col.name}」`);
    });
    box.appendChild(item);
  }
}

function renderStats() {
  const col = currentCollection();
  const dl = $("#stats");
  if (!col) {
    dl.innerHTML = `<div><dt>集合</dt><dd>—</dd></div>`;
    return;
  }
  dl.innerHTML = `
    <div><dt>文档数</dt><dd>${col.doc_count}</dd></div>
    <div><dt>分块数</dt><dd>${col.chunk_count}</dd></div>
    <div><dt>向量维度</dt><dd>${col.dim}</dd></div>
    <div><dt>集合</dt><dd style="font-size:12px">${escapeHtml(col.name)}</dd></div>`;
}

function renderIndex(index) {
  const box = $("#index-list");
  if (!box) return;
  box.innerHTML = (index || [])
    .map((idx) => `<div class="idx"><span>${escapeHtml(idx.name)}</span><span><b>${idx.type}</b> · ${escapeHtml(idx.columns.join(", "))}</span></div>`)
    .join("");
}

/* ------------------------------------------------------------------ */
/* 导入                                                                */
/* ------------------------------------------------------------------ */

async function importDocument() {
  const col = currentCollection();
  if (!col) {
    importLog("请先选择集合", "err");
    return;
  }
  const title = $("#doc-title").value.trim();
  const text = $("#doc-text").value.trim();
  if (!title) return importLog("请填写文档标题", "err");
  if (!text) return importLog("请粘贴或选择要导入的文本", "err");

  state.busy = true;
  $("#btn-import").disabled = true;
  $("#import-progress").classList.remove("hidden");
  const source = state.fileName || "粘贴文本";
  importLog(`开始导入「${escapeHtml(title)}」：${chunkText(text).length} 个分块`);

  try {
    const saved = await ingestDocument(title, text, source);
    if (saved.duplicate) {
      importLog(`内容重复，已跳过（已存在文档「${escapeHtml(saved.document.title)}」）`, "err");
    } else {
      importLog(
        `写入 seekdb 成功：${saved.chunk_count} 个分块 / ${saved.char_count} 字，耗时 ${saved.write_ms} ms`,
        "ok"
      );
      await verifySearchable(saved, title);
    }

    $("#doc-text").value = "";
    $("#doc-title").value = "";
    $("#file-hint").textContent = "或直接粘贴文本";
    $("#chunk-hint").textContent = "0 字";
    state.fileName = "";
    await loadCollections();
    await refreshIndex(false);
    await saveSession();
  } catch (err) {
    importLog(`导入失败：${err.message}`, "err");
  } finally {
    state.busy = false;
    $("#btn-import").disabled = false;
    setTimeout(() => $("#import-progress").classList.add("hidden"), 600);
    $("#import-bar").style.width = "0%";
  }
}

/** 切块 → 本地向量化 → 写入 seekdb，返回后端落库结果。 */
async function ingestDocument(title, text, source) {
  const col = currentCollection();
  const chunks = chunkText(text);
  const vectors = [];
  const batchSize = 4;
  for (let i = 0; i < chunks.length; i += batchSize) {
    const batch = chunks.slice(i, i + batchSize);
    const { vectors: vecs, ms } = await embedder.embed(batch);
    vectors.push(...vecs);
    const done = Math.min(i + batchSize, chunks.length);
    $("#import-bar").style.width = `${(done / chunks.length) * 100}%`;
    importLog(`向量化 ${done}/${chunks.length}（本批 ${ms} ms，本地推理）`);
  }

  const hash = await sha256(text);
  const payload = {
    collection_id: col.id,
    title,
    source,
    content_hash: hash,
    char_count: text.length,
    chunks: chunks.map((chunk, index) => ({
      seq: index,
      text: chunk,
      embedding: vectors[index],
      meta: { chars: chunk.length, model: "bge-small-zh-v1.5", dtype: state.dtype },
    })),
  };
  return api("/api/documents", { method: "POST", body: payload });
}

/** 一键导入 sample_data 下的示例语料（仍走完整的本地向量化链路）。 */
async function loadSamples() {
  if (!currentCollection()) return importLog("请先选择集合", "err");
  const btn = $("#btn-samples");
  btn.disabled = true;
  $("#import-progress").classList.remove("hidden");
  try {
    const samples = await api("/api/samples");
    importLog(`载入示例语料：${samples.length} 篇`);
    for (const sample of samples) {
      const text = await (await fetch(`/samples/${sample.file}`)).text();
      importLog(`→ ${sample.title}`);
      const saved = await ingestDocument(sample.title, text, sample.file);
      if (saved.duplicate) {
        importLog(`  「${sample.title}」已存在，跳过`);
      } else {
        importLog(`  写入 ${saved.chunk_count} 个分块（${saved.char_count} 字，${saved.write_ms} ms）`, "ok");
      }
    }
    await loadCollections();
    await refreshIndex(false);
    await saveSession();
    importLog("示例语料导入完成，可直接检索", "ok");
  } catch (err) {
    importLog(`示例导入失败：${err.message}`, "err");
  } finally {
    btn.disabled = false;
    setTimeout(() => $("#import-progress").classList.add("hidden"), 600);
    $("#import-bar").style.width = "0%";
  }
}


/** 导入后立刻用全文检索验证新内容已可检索（无需重建索引）。 */
async function verifySearchable(saved, title) {
  try {
    const query = title.length >= 2 ? title : saved.title.slice(0, 8);
    const { vectors, ms } = await embedder.embed([query]);
    const res = await api("/api/search", {
      method: "POST",
      body: {
        collection_id: state.collectionId,
        query,
        embedding: vectors[0],
        top_k: 5,
        mode: "text",
      },
    });
    const hit = res.results.some((r) => r.doc_id === saved.id);
    if (hit) {
      importLog(`增量验证：新文档已能被检索命中（query="${escapeHtml(query)}"，SQL ${res.sql_ms} ms，嵌入 ${ms} ms）`, "ok");
    } else {
      importLog(`增量验证：暂未命中（可能标题词过短），可在检索区手动查询`, "");
    }
  } catch (err) {
    importLog(`增量验证失败：${err.message}`, "err");
  }
}

/* ------------------------------------------------------------------ */
/* 检索                                                                */
/* ------------------------------------------------------------------ */

function renderResults(res, query, embeddingMs, mode) {
  const box = $("#results");
  $("#search-meta").innerHTML = `
    <span>嵌入 ${embeddingMs} ms</span>
    <span>seekdb SQL ${res.sql_ms} ms</span>
    <span>命中 ${res.count} 条</span>
    <span>权重 ${res.weights ? res.weights.map((w) => w.toFixed(2)).join(" / ") : "-"}</span>`;
  if (!res.results.length) {
    box.innerHTML = `<div class="empty">没有命中内容。换个问法，或确认该集合已导入语料。</div>`;
    return;
  }
  box.innerHTML = res.results
    .map((item, index) => {
      const vecPct = Math.max(0, Math.min(1, item.vec_score));
      const textPct = Math.max(0, Math.min(1, item.text_score));
      return `
      <article class="result">
        <div class="result-head">
          <div class="result-title">#${index + 1} ${escapeHtml(item.title)}<span class="seq">分块 ${item.seq}</span></div>
          <span class="badge">score ${item.score.toFixed(4)}</span>
        </div>
        <p>${highlight(item.content, query)}</p>
        <div class="scorebar">
          <div class="v" style="width:${vecPct * 100}%"></div>
          <div class="t" style="width:${textPct * 100}%"></div>
        </div>
        <div class="score-detail">
          <span>向量 ${item.vec_score.toFixed(4)}</span>
          <span>全文 ${item.text_score.toFixed(4)}</span>
          <span>模式 ${mode}</span>
          <span>doc ${item.doc_id.slice(0, 10)}</span>
        </div>
      </article>`;
    })
    .join("");
}

async function search() {
  const col = currentCollection();
  if (!col) return log("请先选择集合", "err");
  const query = $("#query").value.trim();
  if (!query) return log("请输入检索问题", "err");

  $("#btn-search").disabled = true;
  $("#search-hint").textContent = "本地计算查询向量…";
  try {
    const { vectors, ms } = await embedder.embed([query]);
    const body = {
      collection_id: col.id,
      query,
      embedding: vectors[0],
      top_k: state.topk,
      mode: state.mode,
    };
    if (state.mode === "hybrid") body.weights = [state.weightVector, 1 - state.weightVector];
    const res = await api("/api/search", { method: "POST", body });
    renderResults(res, query, ms, state.mode);
    $("#search-hint").textContent = "向量 + 全文在 seekdb 内完成";
    log(`检索「${query}」→ ${res.count} 条（嵌入 ${ms} ms / SQL ${res.sql_ms} ms）`, "ok");
    await appendHistory({ query, mode: state.mode, top_k: state.topk, sql_ms: res.sql_ms, embedding_ms: ms, top: res.results.slice(0, 3).map((r) => ({ title: r.title, score: r.score })) });
  } catch (err) {
    log(`检索失败：${err.message}`, "err");
    $("#search-hint").textContent = "检索失败";
  } finally {
    $("#btn-search").disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* 会话状态（存 seekdb）                                                */
/* ------------------------------------------------------------------ */

async function restoreSession() {
  if (!state.collectionId) return;
  try {
    if (state.sessionId) {
      state.session = await api(`/api/sessions/${state.sessionId}`);
    }
  } catch (_) {
    state.sessionId = "";
    localStorage.removeItem("stepseek.session");
  }
  if (!state.session) {
    state.session = await api("/api/sessions", {
      method: "POST",
      body: { collection_id: state.collectionId, title: currentCollection()?.name || "" },
    });
    state.sessionId = state.session.id;
    localStorage.setItem("stepseek.session", state.sessionId);
  }
  $("#session-label").textContent = `会话 ${state.session.id.slice(0, 12)} · 存于 seekdb`;
  renderHistory();
}

function renderHistory() {
  const box = $("#history");
  const items = (state.session?.state?.history || []).slice(0, 12);
  if (!items.length) {
    box.innerHTML = `<li style="cursor:default"><span class="q" style="color:var(--muted)">还没有检索记录</span></li>`;
    return;
  }
  box.innerHTML = items
    .map(
      (h) => `<li data-q="${escapeHtml(h.query)}" data-mode="${h.mode}">
        <span class="q">${escapeHtml(h.query)}</span>
        <span class="m">${h.mode} · ${h.sql_ms}ms</span>
      </li>`
    )
    .join("");
  box.querySelectorAll("li[data-q]").forEach((li) => {
    li.addEventListener("click", () => {
      $("#query").value = li.dataset.q;
      setMode(li.dataset.mode);
      search();
    });
  });
}

async function appendHistory(entry) {
  if (!state.session) return;
  const history = state.session.state?.history || [];
  history.unshift({ ...entry, at: new Date().toISOString() });
  state.session.state = { ...(state.session.state || {}), history: history.slice(0, 20) };
  try {
    await api(`/api/sessions/${state.sessionId}`, {
      method: "PUT",
      body: { state: state.session.state },
    });
    renderHistory();
  } catch (err) {
    log(`会话保存失败：${err.message}`, "err");
  }
}

async function saveSession() {
  if (!state.session) return;
  try {
    await api(`/api/sessions/${state.sessionId}`, {
      method: "PUT",
      body: { state: state.session.state, title: currentCollection()?.name || "" },
    });
  } catch (_) {
    /* ignore */
  }
}

/* ------------------------------------------------------------------ */
/* 索引刷新                                                            */
/* ------------------------------------------------------------------ */

async function refreshIndex(verbose = true) {
  if (!state.collectionId) return;
  try {
    const res = await api(`/api/collections/${state.collectionId}/refresh`, { method: "POST" });
    renderIndex(res.index);
    await loadCollections();
    if (verbose) log(`索引已刷新（ANALYZE ${res.took_ms} ms），新增内容可检索`, "ok");
  } catch (err) {
    if (verbose) log(`刷新失败：${err.message}`, "err");
  }
}

/* ------------------------------------------------------------------ */
/* 量化对比                                                            */
/* ------------------------------------------------------------------ */

const BENCH_SENTENCES = [
  "seekdb 是 OceanBase 推出的轻量级数据库，支持向量检索与全文检索。",
  "浏览器端推理通常使用 WebGPU 或 WASM 两种后端执行 ONNX 模型。",
  "INT8 量化把 32 位浮点权重压缩成 8 位整数，模型体积约为原来的四分之一。",
  "HNSW 是一种近似最近邻索引，适合高维向量的相似度检索。",
  "RAG 应用先把文档切块向量化，再根据问题召回相关段落。",
  "端侧推理的优势是数据不出本机，隐私风险更低，也不依赖网络。",
  "全文检索依赖倒排索引，中文通常需要 ngram 或分词器支持。",
  "混合检索把向量相似度和关键词相关度加权，兼顾语义与精确匹配。",
  "FP16 用半精度浮点存储权重，在支持 WebGPU 的设备上速度更快。",
  "知识库增量导入后，新加入的内容应当无需重建全量索引即可被检索到。",
];

const BENCH_QUERIES = [
  "浏览器里怎么运行推理模型",
  "向量索引 HNSW 是什么",
  "量化之后模型会小多少",
  "新导入的内容为什么能马上被搜到",
];

function cosine(a, b) {
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i += 1) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return dot / (Math.sqrt(na) * Math.sqrt(nb) || 1);
}

function top1(vectors, query) {
  let best = -1;
  let bestScore = -Infinity;
  vectors.forEach((vec, index) => {
    const score = cosine(vec, query);
    if (score > bestScore) {
      bestScore = score;
      best = index;
    }
  });
  return best;
}

async function runBenchmark() {
  const btn = $("#btn-benchmark");
  btn.disabled = true;
  const box = $("#benchmark-result");
  box.innerHTML = `<div class="empty small">正在实测，需依次加载 FP32 / FP16 / INT8 权重…</div>`;

  const original = { device: state.device, dtype: state.dtype };
  const variants = [
    { key: "fp32", dtype: "fp32", device: original.device, label: "FP32" },
    { key: "fp16", dtype: "fp16", device: "webgpu", label: "FP16" },
    { key: "int8", dtype: "q8", device: "wasm", label: "INT8" },
  ];
  const results = [];
  let baseline = null;

  try {
    for (const variant of variants) {
      if (variant.device === "webgpu" && original.device !== "webgpu") {
        results.push({ key: variant.key, label: variant.label, skipped: "当前环境无 WebGPU，跳过 FP16" });
        continue;
      }
      log(`量化实测：加载 ${variant.label}（${variant.device}）…`);
      const ready = await embedder.init(variant.device, variant.dtype);
      if (!ready) {
        results.push({ key: variant.key, label: variant.label, skipped: "加载失败" });
        continue;
      }
      const benchStart = performance.now();
      const sentenceVectors = (await embedder.embed(BENCH_SENTENCES)).vectors;
      const queryVectors = (await embedder.embed(BENCH_QUERIES)).vectors;
      const elapsed = performance.now() - benchStart;
      const perItem = elapsed / (BENCH_SENTENCES.length + BENCH_QUERIES.length);

      let avgCos = 1;
      let minCos = 1;
      let top1Agree = null;
      if (!baseline) {
        baseline = { sentenceVectors, queryVectors };
      } else {
        const sims = [];
        sentenceVectors.forEach((vec, index) => sims.push(cosine(vec, baseline.sentenceVectors[index])));
        avgCos = sims.reduce((a, b) => a + b, 0) / sims.length;
        minCos = Math.min(...sims);
        let agree = 0;
        queryVectors.forEach((q, index) => {
          if (top1(sentenceVectors, q) === top1(baseline.sentenceVectors, baseline.queryVectors[index])) agree += 1;
        });
        top1Agree = `${agree}/${BENCH_QUERIES.length}`;
      }
      results.push({
        key: variant.key,
        label: variant.label,
        device: ready.device,
        dtype: ready.dtype,
        load_ms: ready.load_ms,
        per_item_ms: Math.round(perItem * 10) / 10,
        avg_cosine: Number(avgCos.toFixed(5)),
        min_cosine: Number(minCos.toFixed(5)),
        top1_agreement: top1Agree,
        bytes: state.modelInfo?.files?.[variant.key]?.bytes || 0,
      });
      log(`${variant.label} 完成：${perItem.toFixed(1)} ms/条`, "ok");
    }
  } catch (err) {
    box.innerHTML = `<div class="empty small">实测中断：${escapeHtml(err.message)}</div>`;
    log(`量化实测失败：${err.message}`, "err");
  } finally {
    log(`恢复检索模型：${original.device} / ${original.dtype}`);
    await embedder.init(original.device, original.dtype);
    btn.disabled = false;
  }

  renderBenchmark(results);
  try {
    await api("/api/benchmark/report", {
      method: "POST",
      body: {
        results,
        meta: {
          device: original.device,
          user_agent: navigator.userAgent,
          model: state.modelInfo?.model || "bge-small-zh-v1.5",
          sentences: BENCH_SENTENCES.length,
          queries: BENCH_QUERIES.length,
        },
      },
    });
    log("量化实测结果已保存到 docs/quantization-results.json", "ok");
  } catch (err) {
    log(`量化结果保存失败：${err.message}`, "err");
  }
}

function renderBenchmark(results) {
  const rows = results
    .map((r) => {
      if (r.skipped) {
        return `<tr><td>${r.label}</td><td colspan="6" class="hint">${escapeHtml(r.skipped)}</td></tr>`;
      }
      const isBest = r.key === "int8";
      return `<tr class="${isBest ? "best" : ""}">
        <td>${r.label}</td>
        <td class="num">${fmtBytes(r.bytes)}</td>
        <td class="num">${r.per_item_ms} ms</td>
        <td class="num">${r.avg_cosine.toFixed(4)}</td>
        <td class="num">${r.min_cosine.toFixed(4)}</td>
        <td class="num">${r.top1_agreement || "基准"}</td>
        <td class="num">${r.device}</td>
      </tr>`;
    })
    .join("");
  $("#benchmark-result").innerHTML = `
    <table class="bench">
      <thead><tr><th>精度</th><th>体积</th><th>单条耗时</th><th>平均余弦</th><th>最低余弦</th><th>Top1 一致</th><th>后端</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="hint" style="margin-top:8px">余弦/一致率均以本机 FP32 结果为基准计算。</p>`;
}

/* ------------------------------------------------------------------ */
/* 事件绑定                                                            */
/* ------------------------------------------------------------------ */

function setMode(mode) {
  state.mode = mode;
  $$("#mode-seg button").forEach((btn) => btn.classList.toggle("active", btn.dataset.mode === mode));
  $("#weight-field").classList.toggle("hidden", mode !== "hybrid");
}

function bindEvents() {
  $("#btn-search").addEventListener("click", search);
  $("#query").addEventListener("keydown", (event) => {
    if (event.key === "Enter") search();
  });
  $("#btn-import").addEventListener("click", importDocument);
  $("#btn-samples").addEventListener("click", loadSamples);
  $("#btn-refresh").addEventListener("click", () => refreshIndex(true));
  $("#btn-benchmark").addEventListener("click", runBenchmark);
  $("#btn-clear-log").addEventListener("click", () => ($("#sys-log").innerHTML = ""));

  $("#btn-new-collection").addEventListener("click", () => $("#collection-form").classList.remove("hidden"));
  $("#btn-cancel-collection").addEventListener("click", () => $("#collection-form").classList.add("hidden"));
  $("#collection-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = $("#collection-name").value.trim();
    if (!name) return;
    const created = await api("/api/collections", {
      method: "POST",
      body: { name, description: $("#collection-desc").value.trim() },
    });
    state.collectionId = created.id;
    localStorage.setItem("stepseek.collection", created.id);
    state.sessionId = "";
    localStorage.removeItem("stepseek.session");
    $("#collection-name").value = "";
    $("#collection-desc").value = "";
    $("#collection-form").classList.add("hidden");
    await loadCollections();
    await restoreSession();
    log(`已创建集合「${name}」`, "ok");
  });

  $$("#mode-seg button").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });

  $("#topk").addEventListener("change", (event) => {
    state.topk = Number(event.target.value);
  });

  $("#weight-vector").addEventListener("input", (event) => {
    state.weightVector = Number(event.target.value);
    $("#weight-label").textContent = `${state.weightVector.toFixed(2)} / ${(1 - state.weightVector).toFixed(2)}`;
  });

  $("#doc-text").addEventListener("input", (event) => {
    const text = event.target.value;
    $("#chunk-hint").textContent = `${text.length} 字 · ${chunkText(text).length} 块`;
  });

  $("#file-input").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    const text = await file.text();
    state.fileName = file.name;
    $("#doc-text").value = text;
    $("#doc-text").dispatchEvent(new Event("input"));
    if (!$("#doc-title").value.trim()) {
      $("#doc-title").value = file.name.replace(/\.(txt|md|markdown)$/i, "");
    }
    $("#file-hint").textContent = `${file.name} · ${(file.size / 1024).toFixed(1)} KB`;
  });
}

init();
