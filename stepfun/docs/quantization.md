# 量化方案与实测

## 模型来源

| 项目 | 内容 |
| --- | --- |
| 原始模型 | [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)，12 层 BERT，24M 参数，输出 512 维 |
| 转换版本 | `Xenova/bge-small-zh-v1.5`（transformers.js 官方转换，ONNX 格式） |
| 下载方式 | `python scripts/prepare_assets.py`，走 `hf-mirror.com` 镜像，落盘到 `app/static/models/bge-small-zh-v1.5/` |
| 运行时 | ONNX Runtime Web 1.21（随 transformers.js 3.8 打包），浏览器内推理，无云端调用 |

三份权重的生成方式：

| 精度 | 文件名 | 量化方式 | 体积 |
| --- | --- | --- | --- |
| FP32 | `onnx/model.onnx` | 原始导出 | 94.9 MB |
| FP16 | `onnx/model_fp16.onnx` | 权重半精度（IEEE 754 binary16），仅 WebGPU 使用 | 47.5 MB |
| INT8 | `onnx/model_quantized.onnx` | 每通道动态 8bit 量化（transformers.js `dtype=q8`） | 24.0 MB |

体积对比：FP16 为 FP32 的 **50%**，INT8 为 FP32 的 **25%**，INT8 相对 FP16 再减半。

## 质量评估方法

页面右上「量化对比 → 运行实测」在本机浏览器内完成，流程：

1. 固定 10 条中文句子（覆盖数据库、端侧推理、量化、RAG 等主题）与 4 条检索问句；
2. 依次切换 FP32 → FP16 → INT8 三份权重，用同一 tokenizer（`pooling=cls`、`normalize=true`）编码；
3. 以 FP32 结果为基准，计算：
   - **平均余弦 / 最低余弦**：同一条句子在量化模型与 FP32 下向量的余弦相似度；
   - **Top-1 一致率**：4 条问句在 10 条句子上做余弦排序，量化模型的首位是否与 FP32 相同；
   - **单条耗时**：编码总耗时 / 条数（含前后处理）；
4. 结果 POST 到 `/api/benchmark/report`，落盘 `docs/quantization-results.json`。

## 实测结果

实测环境：Windows + Chrome 142 内核（支持 WebGPU），`bge-small-zh-v1.5`，10 条句集 + 4 条问句，原始数据见 `docs/quantization-results.json`。

| 精度 | 体积 | 单条耗时 | 平均余弦 | 最低余弦 | Top-1 一致率 | 执行后端 | 加载耗时 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| FP32 | 94.9 MB | 40.9 ms | 1.00000（基准） | 1.00000 | 基准 | WebGPU | 580 ms |
| FP16 | 47.5 MB | 2.8 ms | 0.99997 | 0.99997 | 4/4 | WebGPU | 91 ms |
| INT8 | 24.0 MB | 6.6 ms | 0.98991 | 0.98741 | 4/4 | WASM | 190 ms |

解读：

- **体积**：FP16 是 FP32 的 50.0%，INT8 是 FP32 的 25.3%，与理论值一致。
- **质量**：FP16 与 FP32 的向量几乎重合（余弦 0.99997）；INT8 平均余弦 0.9899、最低 0.9874，仍属可用区间，4 条问句的 Top-1 结果与 FP32 完全一致，说明量化没有改变检索排序。
- **速度**：FP16 在 WebGPU 上单条 2.8 ms，比 FP32 快约 14 倍；INT8 跑在 WASM(CPU) 上为 6.6 ms。注意 FP32/FP16 是 GPU、INT8 是 CPU，本表反映的是「实际默认部署组合」而不是同后端横向对比；若把 FP32 也放到 WASM，单条耗时会明显更高。
- **结论**：端侧默认 `WebGPU + FP16`，无 WebGPU 时 `WASM + INT8`，两者都保持了与 FP32 一致的检索排序。

## 结论与取舍

- 端侧默认策略：**支持 WebGPU 用 FP16，不支持则用 WASM + INT8**。FP16 在 GPU 上吞吐更高，INT8 在 CPU 上体积与速度最优。
- 三种精度的检索排序保持一致（Top-1 一致率见实测表），说明量化没有破坏语义区分度；如需更严格评估，可扩充问句集并统计 Recall@5。
- 若要进一步压缩，可考虑 `q4`（4bit）权重，但 512 维 embedding 对权重量化更敏感，需重新做一致性验证。
