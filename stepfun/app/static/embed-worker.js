/**
 * Embedding Worker：所有向量都在浏览器本地计算（WebGPU 优先，WASM 降级），
 * 不调用任何云端推理接口。模型文件由本机 FastAPI 静态目录提供。
 */
import { pipeline, env } from "/vendor/transformers.min.js";

const MODEL_ID = "bge-small-zh-v1.5";

env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = "/models/";
env.backends.onnx.wasm.wasmPaths = "/vendor/";

let extractor = null;
let current = { device: null, dtype: null };

function post(msg) {
  self.postMessage(msg);
}

function describeError(err) {
  return (err && (err.message || err.toString())) || "unknown error";
}

async function build(device, dtype) {
  const started = performance.now();
  post({ type: "status", phase: "loading", device, dtype });
  const instance = await pipeline("feature-extraction", MODEL_ID, {
    device,
    dtype,
    progress_callback: (p) => {
      if (p && p.status === "progress" && p.file) {
        post({
          type: "status",
          phase: "download",
          file: p.file,
          progress: Math.round(p.progress || 0),
        });
      }
    },
  });
  const loadMs = performance.now() - started;
  extractor = instance;
  current = { device, dtype };
  return loadMs;
}

async function init({ device, dtype }) {
  if (extractor && current.device === device && current.dtype === dtype) {
    post({ type: "ready", device: current.device, dtype: current.dtype, load_ms: 0, reused: true });
    return;
  }
  if (extractor && typeof extractor.dispose === "function") {
    try {
      await extractor.dispose();
    } catch (_) {
      /* ignore */
    }
    extractor = null;
  }

  try {
    const loadMs = await build(device, dtype);
    post({ type: "ready", device, dtype, load_ms: Math.round(loadMs), fallback: false });
  } catch (err) {
    if (device === "webgpu") {
      post({
        type: "status",
        phase: "fallback",
        message: `WebGPU 初始化失败（${describeError(err)}），自动降级为 WASM`,
      });
      try {
        if (extractor && typeof extractor.dispose === "function") {
          await extractor.dispose();
        }
      } catch (_) {
        /* ignore */
      }
      extractor = null;
      try {
        const loadMs = await build("wasm", "q8");
        post({
          type: "ready",
          device: "wasm",
          dtype: "q8",
          load_ms: Math.round(loadMs),
          fallback: true,
          reason: describeError(err),
        });
        return;
      } catch (err2) {
        post({ type: "error", scope: "init", message: describeError(err2) });
        return;
      }
    }
    post({ type: "error", scope: "init", message: describeError(err) });
  }
}

async function embed(texts) {
  if (!extractor) throw new Error("模型尚未就绪");
  const started = performance.now();
  const output = await extractor(texts, { pooling: "cls", normalize: true });
  const ms = performance.now() - started;
  return { vectors: output.tolist(), ms: Math.round(ms * 10) / 10 };
}

self.onmessage = async (event) => {
  const msg = event.data || {};
  if (msg.type === "init") {
    await init(msg);
    return;
  }
  if (msg.type === "embed") {
    try {
      const { vectors, ms } = await embed(msg.texts);
      post({ type: "embedded", id: msg.id, vectors, ms });
    } catch (err) {
      post({ type: "error", scope: "embed", id: msg.id, message: describeError(err) });
    }
    return;
  }
  if (msg.type === "dispose") {
    if (extractor && typeof extractor.dispose === "function") {
      try {
        await extractor.dispose();
      } catch (_) {
        /* ignore */
      }
    }
    extractor = null;
    current = { device: null, dtype: null };
    post({ type: "disposed" });
  }
};
