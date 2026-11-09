"""向量编码：bge-small-zh-v1.5（512 维，CLS pooling + L2 归一化）。

优先使用 ONNX Runtime；模型文件缺失时自动从 Hugging Face 下载；
完全离线等异常情况下退化为字符 n-gram 哈希向量，保证服务始终可用，
当前模式会在 /v1/health 中如实返回。
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading

import numpy as np

logger = logging.getLogger("memory_bar.embedding")

_MODEL_FILES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "config.json",
    "onnx/model_quantized.onnx",
)


class Embedder:
    def __init__(self, model_dir: str, dim: int = 512, repo: str = "Xenova/bge-small-zh-v1.5"):
        self.model_dir = model_dir
        self.dim = dim
        self.repo = repo
        self.mode = "hash"
        self.model_name = "bge-small-zh-v1.5"
        self._session = None
        self._tokenizer = None
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------ load
    def _ensure_files(self) -> None:
        tokenizer = os.path.join(self.model_dir, "tokenizer.json")
        onnx = os.path.join(self.model_dir, "onnx", "model_quantized.onnx")
        onnx_fp32 = os.path.join(self.model_dir, "onnx", "model.onnx")
        if os.path.exists(tokenizer) and (os.path.exists(onnx) or os.path.exists(onnx_fp32)):
            return
        from huggingface_hub import snapshot_download

        logger.info("下载向量模型 %s 到 %s …", self.repo, self.model_dir)
        snapshot_download(
            repo_id=self.repo,
            local_dir=self.model_dir,
            allow_patterns=list(_MODEL_FILES),
        )

    def _load(self) -> None:
        try:
            self._ensure_files()
            import onnxruntime as ort
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(os.path.join(self.model_dir, "tokenizer.json"))
            tokenizer.enable_truncation(max_length=256)
            tokenizer.enable_padding()

            quantized = os.path.join(self.model_dir, "onnx", "model_quantized.onnx")
            model_path = quantized if os.path.exists(quantized) else os.path.join(
                self.model_dir, "onnx", "model.onnx"
            )
            session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            self._tokenizer = tokenizer
            self._session = session
            self._input_names = {item.name for item in session.get_inputs()}
            self.mode = "onnx"
            logger.info("向量模型就绪：%s（%s）", self.model_name, os.path.basename(model_path))
        except Exception as exc:  # noqa: BLE001 - 离线时降级，不让服务起不来
            logger.warning("ONNX 模型不可用（%s），退化为哈希向量", exc)
            self.mode = "hash"

    # ----------------------------------------------------------------- embed
    def _onnx_embed(self, texts: list[str]) -> list[list[float]]:
        encodings = self._tokenizer.encode_batch(texts)
        input_ids = np.array([enc.ids for enc in encodings], dtype=np.int64)
        attention_mask = np.array([enc.attention_mask for enc in encodings], dtype=np.int64)
        feeds = {"input_ids": input_ids, "attention_mask": attention_mask}
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)
        outputs = self._session.run(None, feeds)
        hidden = outputs[0]
        cls = hidden[:, 0, :].astype(np.float32)
        norms = np.linalg.norm(cls, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (cls / norms).tolist()

    def _hash_embed(self, text: str) -> list[float]:
        vec = np.zeros(self.dim, dtype=np.float32)
        compact = "".join(text.split()).lower() or "空"
        grams = [compact[i : i + 2] for i in range(max(1, len(compact) - 1))] or [compact]
        for gram in grams:
            digest = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:8], 16)
            vec[digest % self.dim] += 1.0 if (digest >> 8) & 1 else -1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec.tolist()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.mode == "onnx":
            with self._lock:
                return self._onnx_embed(texts)
        return [self._hash_embed(text) for text in texts]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def info(self) -> dict:
        return {"model": self.model_name, "dim": self.dim, "mode": self.mode}
