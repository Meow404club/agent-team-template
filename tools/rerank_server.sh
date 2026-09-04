#!/usr/bin/env bash
# 精排服务：llama-server rerank 端点 + Reranker GGUF（示例 Qwen3-Reranker-0.6B Q8）。
# 与嵌入服务（端口 8937）并存；同族 tokenizer，原生 /v1/rerank 端点。
set -euo pipefail

LLAMA_BIN="${LLAMA_BIN:?set LLAMA_BIN to your llama-server binary}"
MODEL="${MODEL:?set MODEL to your reranker GGUF, e.g. tmp/models/Qwen3-Reranker-0.6B.Q8_0.gguf}"
PORT="${PORT:-8938}"
HOST="${HOST:-127.0.0.1}"
# 调用量小 → 少槽多上下文：单请求可吞 ~2k token 的 (query+候选)
SLOTS="${SLOTS:-4}"
CTX_PER_SLOT="${CTX_PER_SLOT:-6144}"
CTX=$((SLOTS * CTX_PER_SLOT))
UBATCH="${UBATCH:-4096}"   # 物理批上限：rerank 输入是整段 (query+doc)，必须 ≥ 单输入 token 数

# AMD/HIP 环境变量（NVIDIA/CPU 环境可删除这三行）
# RSS 行为实测：变长请求使 llama.cpp 把 compute/scratch 缓冲钉在历史最大负载上
#（有界不泄漏、也永不归还）——超限用 services.sh doctor 重启。
# 下面两个 env 只压 glibc 碎片，对 llama.cpp 自留缓冲无效。
export MALLOC_ARENA_MAX=2
export MALLOC_TRIM_THRESHOLD_=134217728
export HSA_ENABLE_DXG_DETECTION=1
export KFD_NPS_RELAX=1
export PATH="$PATH:/opt/rocm/bin"

exec "$LLAMA_BIN" \
  --model "$MODEL" \
  --rerank \
  --parallel "$SLOTS" \
  --ctx-size "$CTX" \
  --batch-size 1024 \
  --port "$PORT" \
  --host "$HOST" \
  --threads 8 \
  --flash-attn on \
  --jinja
