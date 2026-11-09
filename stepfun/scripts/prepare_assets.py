"""一键准备前端资产：transformers.js / onnxruntime-web（npm tarball）与 bge-small-zh-v1.5（三种精度 ONNX）。

用法：
    python scripts/prepare_assets.py            # 缺什么补什么
    python scripts/prepare_assets.py --force    # 全部重新下载

说明：
  - 模型来自 hf-mirror.com（HuggingFace 镜像），来源仓库 BAAI/bge-small-zh-v1.5 的 transformers.js 转换版 Xenova/bge-small-zh-v1.5
  - 前端库直接解包 npm registry 的 tarball，不依赖 node/npm 环境
"""

from __future__ import annotations

import argparse
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
VENDOR = STATIC / "vendor"
MODEL = STATIC / "models" / "bge-small-zh-v1.5"

HF_MIRROR = "https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main"
MODEL_FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
    "onnx/model.onnx",          # FP32
    "onnx/model_fp16.onnx",     # FP16
    "onnx/model_quantized.onnx" # INT8 (q8)
]

NPM_PACKAGES = [
    # (tarball, 需要解出的文件 -> 目标文件名)
    (
        "https://registry.npmjs.org/@huggingface/transformers/-/transformers-3.8.1.tgz",
        {"package/dist/transformers.min.js": "transformers.min.js"},
    ),
    (
        "https://registry.npmjs.org/onnxruntime-web/-/onnxruntime-web-1.21.0.tgz",
        {
            "package/dist/ort-wasm-simd-threaded.jsep.wasm": "ort-wasm-simd-threaded.jsep.wasm",
            "package/dist/ort-wasm-simd-threaded.jsep.mjs": "ort-wasm-simd-threaded.jsep.mjs",
            "package/dist/ort.bundle.min.mjs": "ort.bundle.min.mjs",
        },
    ),
]


def download(url: str, dest: Path, force: bool = False) -> None:
    if dest.exists() and dest.stat().st_size > 0 and not force:
        print(f"  skip {dest.relative_to(ROOT)} ({dest.stat().st_size:,} bytes)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            fh.write(block)
            done += len(block)
            if total:
                print(f"\r  {dest.name}: {done * 100 / total:5.1f}%  {done:,}/{total:,}", end="")
    print()
    tmp.replace(dest)
    print(f"  ok   {dest.relative_to(ROOT)} ({dest.stat().st_size:,} bytes)")


def extract_tarball(url: str, members: dict[str, str], force: bool = False) -> None:
    VENDOR.mkdir(parents=True, exist_ok=True)
    pending = {src: dst for src, dst in members.items() if force or not (VENDOR / dst).exists()}
    if not pending:
        print(f"  skip {url.split('/')[-1]} (已存在)")
        return
    print(f"  fetch {url}")
    with urllib.request.urlopen(url, timeout=180) as resp:
        blob = resp.read()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for src, dst in pending.items():
            handle = tar.extractfile(src)
            if handle is None:
                raise RuntimeError(f"tarball 中缺少 {src}")
            target = VENDOR / dst
            target.write_bytes(handle.read())
            print(f"  ok   vendor/{dst} ({target.stat().st_size:,} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    print("[1/2] 下载 bge-small-zh-v1.5（FP32 / FP16 / INT8）")
    for rel in MODEL_FILES:
        download(f"{HF_MIRROR}/{rel}", MODEL / rel, args.force)

    print("[2/2] 解包 transformers.js 与 onnxruntime-web")
    for url, members in NPM_PACKAGES:
        extract_tarball(url, members, args.force)

    print("\n资产准备完成：")
    for path in sorted(list(MODEL.rglob("*")) + list(VENDOR.glob("*"))):
        if path.is_file():
            print(f"  {path.relative_to(ROOT)}  {path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
