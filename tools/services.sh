#!/usr/bin/env bash
# 服务总线：统一管理 brain MCP(HTTP :8939) + llama embed(:8937) + llama rerank(:8938)。
# 用法: tools/services.sh {start|stop|restart|status} [brain|embed|rerank|all]
#   start          # 默认 all：起全部未运行的服务
#   start embed    # 只起指定服务
# 注意：llama /health 在模型加载期返回 503，探活以 HTTP 200 为准。
set -uo pipefail

TOOLS_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$TOOLS_DIR/../tmp/index"
LLAMA_BIN="${LLAMA_BIN:-}"   # llama-server 二进制路径；embed/rerank 需要，brain 不需要
MODEL_DIR="$TOOLS_DIR/../tmp/models"
RERANK_MODEL="${RERANK_MODEL:-$MODEL_DIR/Qwen3-Reranker-0.6B.Q8_0.gguf}"

# AMD/HIP 环境变量（NVIDIA/CPU 环境可删除）
export HSA_ENABLE_DXG_DETECTION=1 KFD_NPS_RELAX=1

services=(brain embed rerank)

port_of() {
  case $1 in
    brain)  echo "${BRAIN_PORT:-8939}" ;;
    embed)  echo "${EMBED_PORT:-8937}" ;;
    rerank) echo "${RERANK_PORT:-8938}" ;;
  esac
}
pidfile_of() { echo "$LOG_DIR/$1-server.pid"; }
log_of()     { echo "$LOG_DIR/$1-server.log"; }

is_up() {  # 仅 200 视为就绪（llama 加载模型期 /health 是 503）
  [[ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$(port_of "$1")/health" 2>/dev/null)" == "200" ]]
}

start_one() {
  local svc=$1
  if is_up "$svc"; then echo "$svc: already running :$(port_of "$svc")"; return 0; fi
  mkdir -p "$LOG_DIR"
  case $svc in
    brain)  # 常驻 HTTP MCP（stdlib 实现，无第三方依赖）
      nohup "$TOOLS_DIR/.venv/bin/python" "$TOOLS_DIR/brain/server.py" >> "$(log_of "$svc")" 2>&1 & ;;
    embed)  # embed_server.sh 自带参数与 HIP 环境，exec 到 llama-server
      [[ -n "$LLAMA_BIN" ]] || { echo "embed: 需要 LLAMA_BIN 环境变量指向 llama-server"; return 1; }
      nohup "$TOOLS_DIR/embed_server.sh" >> "$(log_of "$svc")" 2>&1 & ;;
    rerank)
      [[ -n "$LLAMA_BIN" ]] || { echo "rerank: 需要 LLAMA_BIN 环境变量指向 llama-server"; return 1; }
      nohup "$LLAMA_BIN" \
        --model "$RERANK_MODEL" --rerank \
        --parallel 4 --ctx-size 24576 -b 4096 -ub 4096 \
        --port "$(port_of rerank)" --host 127.0.0.1 --threads 8 --flash-attn on --jinja \
        >> "$(log_of "$svc")" 2>&1 & ;;
    *) echo "unknown service: $svc"; return 1 ;;
  esac
  echo $! > "$(pidfile_of "$svc")"
  local i
  for i in $(seq 1 90); do is_up "$svc" && break; sleep 0.5; done  # llama 加载模型最多等 45s
  if is_up "$svc"; then
    echo "$svc: started (pid $(cat "$(pidfile_of "$svc")")) :$(port_of "$svc")"
  else
    echo "$svc: FAILED, see $(log_of "$svc")"; return 1
  fi
}

stop_one() {
  local svc=$1 port; port=$(port_of "$svc")
  local pf; pf="$(pidfile_of "$svc")"
  if [[ -f "$pf" ]]; then
    kill "$(cat "$pf")" 2>/dev/null || true
    rm -f "$pf"
  fi
  # 按监听端口精准兜底（不误杀无关进程）
  local pids
  pids=$(ss -tlnp 2>/dev/null | grep -E "[:.]$port " | grep -oP 'pid=\K[0-9]+' | sort -u)
  [[ -n "$pids" ]] && kill $pids 2>/dev/null || true
  echo "$svc: stopped"
}

status_one() {
  local svc=$1
  if is_up "$svc"; then
    printf "%-8s UP   :%s  %s\n" "$svc" "$(port_of "$svc")" "$(curl -s -m 2 "http://127.0.0.1:$(port_of "$svc")/health")"
  else
    printf "%-8s DOWN :%s\n" "$svc" "$(port_of "$svc")"
  fi
}

main() {
  local cmd="${1:-status}" svc="${2:-all}"
  shift 2>/dev/null || true
  case "$cmd" in
    start|stop|restart)
      [[ "$svc" == "all" ]] && local targets=("${services[@]}") || local targets=("$svc")
      local rc=0 s
      for s in "${targets[@]}"; do
        if [[ "$cmd" == "restart" ]]; then stop_one "$s"; sleep 1; start_one "$s" || rc=1
        elif [[ "$cmd" == "start" ]]; then start_one "$s" || rc=1
        else stop_one "$s"; fi
      done
      exit $rc
      ;;
    status)
      local s; for s in "${services[@]}"; do status_one "$s"; done
      ;;
    *) echo "usage: $0 {start|stop|restart|status} [brain|embed|rerank|all]"; exit 1 ;;
  esac
}

main "$@"
