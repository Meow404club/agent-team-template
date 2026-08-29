#!/usr/bin/env bash
# 一键初始化：venv → 依赖 → 本地配置 → git 钩子 → 冒烟测试
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> [1/5] Python venv (tools/.venv)"
if [ ! -x tools/.venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv tools/.venv
  else
    python3 -m venv tools/.venv
  fi
fi

echo "==> [2/5] 安装依赖"
if command -v uv >/dev/null 2>&1; then
  uv pip install --python tools/.venv/bin/python -r tools/requirements.txt
else
  tools/.venv/bin/pip install -r tools/requirements.txt
fi

echo "==> [3/5] 生成本地配置（已存在则跳过）"
[ -f tools/config.json ] || cp tools/config.example.json tools/config.json
[ -f tools/sources.json ] || cp tools/sources.example.json tools/sources.json

echo "==> [4/5] 安装 git 钩子 (core.hooksPath=.githooks)"
if git rev-parse --git-dir >/dev/null 2>&1; then
  git config core.hooksPath .githooks
else
  echo "  (尚未 git init —— git init 后重跑本脚本即可安装钩子)"
fi

echo "==> [5/5] 冒烟测试"
tools/.venv/bin/python - <<'PY'
import sys
sys.path.insert(0, "tools")
import brain.server  # noqa: F401  (imports db/embed/search/memory)
print("brain server import OK")
PY

cat <<'NEXT'

完成。接下来三处必改（详见 README「必改清单」）：
  1. tools/config.json     —— 嵌入 API（本地 llama-server 或远端中转）+ query_instruction
  2. tools/sources.json    —— 资料源注册表；改完跑:
       tools/.venv/bin/python tools/brain/index.py all
  3. AGENTS.md + .zcode/agents/*.md —— 占位符换成你的 sources key 与领域红线

可选：本地 GPU 推理 tools/embed_server.sh + tools/rerank_server.sh（见 tools/README.md）
可选：PreToolUse 钩子注册到用户级（见 .githooks/README.md）
NEXT
