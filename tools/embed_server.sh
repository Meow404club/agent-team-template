#!/usr/bin/env bash
# 嵌入服务：llama-server + 嵌入模型 GGUF（示例 Qwen3-Embedding-4B Q8）。
# 单权重多槽并发：--parallel N 让多个请求共享一份权重。
# 注意：llama.cpp 的 --ctx-size 是【总 KV 上下文】，会被槽平分（n_ctx_slot = CTX / N）。
set -euo pipefail

LLAMA_BIN="${LLAMA_BIN:?set LLAMA_BIN to your llama-server binary}"
MODEL="${MODEL:?set MODEL to your embedding GGUF, e.g. tmp/models/Qwen3-Embedding-4B-Q8_0.gguf}"
PORT="${PORT:-8937}"
HOST="${HOST:-127.0.0.1}"
SLOTS="${SLOTS:-12}"                        # 并发槽（嵌入请求短平快，槽越多 GPU 利用率越高）
CTX_PER_SLOT="${CTX_PER_SLOT:-2048}"        # 每槽上下文：块 ≤2000 字符 ≈ ≤700 token
CTX=$((SLOTS * CTX_PER_SLOT))

# AMD/HIP 环境变量（NVIDIA/CPU 环境可删除这三行）
export HSA_ENABLE_DXG_DETECTION=1
export KFD_NPS_RELAX=1
export PATH="$PATH:/opt/rocm/bin"

exec "$LLAMA_BIN" \
  --model "$MODEL" \
  --embedding \
  --pooling last \
  --embd-normalize 2 \
  --parallel "$SLOTS" \
  --ctx-size "$CTX" \
  --port "$PORT" \
  --host "$HOST" \
  --threads 8 \
  --flash-attn on \
  --jinja
