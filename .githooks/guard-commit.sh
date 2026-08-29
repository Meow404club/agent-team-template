#!/usr/bin/env bash
# PreToolUse(Bash) 钩子：拦截不带 --gpg-sign 的 git commit。
# 输入: stdin 上的 JSON（含 tool_input.command）；输出 JSON 决定放行/拦截。
#
# 作用域自检（无需硬编码路径，可安全注册到用户级 ~/.zcode/cli/config.json）：
#   1. 仅当 cwd 所在 git 仓库的主仓库根存在 .githooks/commit-msg（选择加入标记）时生效；
#   2. 作用范围 = 主仓库根 + ../<仓库名>-trees/ worktree 约定目录。
# 因此挂用户级等价于项目级约束，且不影响其他仓库。
input=$(cat)
cmd=$(printf '%s' "$input" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
    print(d.get("tool_input",{}).get("command",""))
except Exception:
    print("")
' 2>/dev/null)

case "$cmd" in
  *"git commit"*) ;;
  *) exit 0 ;;
esac

# 解析主仓库根（兼容 worktree 内执行）
common=$(git rev-parse --git-common-dir 2>/dev/null) || exit 0
[ -n "$common" ] || exit 0
case "$common" in
  /*) ;;
  *) common="$PWD/$common" ;;
esac
MAIN_ROOT=$(cd "$common/.." && pwd)

# 选择加入标记：仓库启用 .githooks 才拦截
[ -f "$MAIN_ROOT/.githooks/commit-msg" ] || exit 0

TREES_DIR="$(dirname "$MAIN_ROOT")/$(basename "$MAIN_ROOT")-trees"
case "$PWD" in
  "$MAIN_ROOT"|"$MAIN_ROOT"/*) ;;
  "$TREES_DIR"|"$TREES_DIR"/*) ;;
  *) exit 0 ;;
esac

case "$cmd" in
  *--gpg-sign*|*"-S "*) exit 0 ;;
  *)
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"提交必须 GPG 签名：请使用 git commit --gpg-sign -S（并在 commit message 中包含 Signoff/Task 标签）。这是本项目的硬约束。"}}'
    exit 0
    ;;
esac
