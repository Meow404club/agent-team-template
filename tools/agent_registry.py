#!/usr/bin/env python3
"""agent_registry.py — 扫描 ZCode 的 agents 目录，列出全部 subagent 的权威状态。

动机（2026-10-08 撞车事故 id1655）：后台 subagent 跨会话压缩存活，主会话凭
"WIP 零提交+SendMessage 失败"误判原作死亡、重派续作，两 coder 同 worktree 并发
约 40 分钟。ZCode 框架保证 agent 终态（完成/停止/出错）必有 task-notification
——没收到通知就是还活着，不存在"无通知死亡"。本工具给编排者一个压缩恢复后/
派发重派前的硬清单：谁还 status=running（含旧会话存活的），它领的是哪张卡、
在哪个 worktree 干活。

数据源：~/.zcode/cli/agents/sess_*/agent_*/metadata.json 的 status 字段
（running/completed/stopped/...）——这是 ZCode 自己写的权威状态，不是推断。
零 ps 扫描（subagent 不是时时在跑命令，进程缺失不代表死亡）。

用法：
  python3 tools/agent_registry.py              # 只列 status=running 的（派发前必查）
  python3 tools/agent_registry.py --all        # 含终态，按 SLUG 查状态史
  python3 tools/agent_registry.py --json       # 机器可读（主会话/后续 hook 消费）
  python3 tools/agent_registry.py --slug X     # 过滤：某 SLUG 的 agent 状态史

过滤规则：running 且 updatedAt 超过 48h 的条目视为崩溃/断电残留（上下文限制下
一个会话不可能跑这么久），所有视图均隐藏；completed/stopped 终态条目全保留。
"""
import argparse
import json
import os
import re
import signal
import sys
from datetime import datetime, timezone

AGENTS_ROOT = os.path.expanduser("~/.zcode/cli/agents")

# running 条目的年龄上限：上下文限制下一个会话不可能跑这么久，超龄 running =
# 崩溃/断电时代的僵尸残留（框架正常运行时终态必有通知，无通知不会真死）
RUNNING_MAX_AGE_H = 48


def read_meta(meta_path):
    try:
        with open(meta_path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def parse_ts(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_str(ts):
    if ts is None:
        return "?"
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return "0s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60}m"


def extract_card_hints(meta):
    """从 prompt 提取 SLUG 与 worktree 线索（纯展示用，缺失则留空）。"""
    prompt = meta.get("prompt") or ""
    slug = None
    m = re.search(r"^SLUG:\s*(\S+)", prompt, re.M)
    if m:
        slug = m.group(1)
    # 本框架 worktree 约定 ../<仓库名>-trees/<slug>——任意 *-trees/ 前缀都认
    m = re.search(r"-trees/([A-Za-z0-9_.-]+)", prompt)
    worktree = m.group(1) if m else ""
    return slug, worktree


def scan():
    if not os.path.isdir(AGENTS_ROOT):
        return []
    rows = []
    for sess in sorted(os.listdir(AGENTS_ROOT)):
        sess_dir = os.path.join(AGENTS_ROOT, sess)
        if not (sess.startswith("sess_") and os.path.isdir(sess_dir)):
            continue
        for agent in sorted(os.listdir(sess_dir)):
            if not agent.startswith("agent_"):
                continue
            meta = read_meta(os.path.join(sess_dir, agent, "metadata.json"))
            if meta is None:
                continue
            slug, worktree = extract_card_hints(meta)
            rows.append({
                # 完整 agent_<uuid>——SendMessage 的 to 字段直接可用（短 id 必然解析失败，id1653）
                "agentId": agent,
                "session": sess[:18],
                "status": meta.get("status", "?"),
                "profile": meta.get("profileId", "?"),
                "description": (meta.get("description") or "")[:46],
                "slug": slug or "",
                "worktree": worktree,
                "createdAt": meta.get("createdAt", ""),
                "updatedAt": meta.get("updatedAt", ""),
                "createdAge": age_str(parse_ts(meta.get("createdAt"))),
                "updatedAge": age_str(parse_ts(meta.get("updatedAt"))),
            })
    # 最新活动在前：当前在跑的沉不到史前崩溃僵尸（900h 级 running=WSL 崩溃时代残留）后面
    rows.sort(key=lambda r: r["updatedAt"], reverse=True)
    return rows


def is_aged_out(row):
    """running 且 updatedAt 距今超过 48h = 僵尸残留，默认不显示（终态条目不受此滤）。"""
    if row["status"] != "running":
        return False
    ts = parse_ts(row["updatedAt"])
    if ts is None:
        return False
    return (datetime.now(timezone.utc) - ts).total_seconds() > RUNNING_MAX_AGE_H * 3600


def main():
    # head 截断管道属正常用法（BrokenPipeError 不当错误报）
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(description="ZCode subagent 状态登记表（扫 agents 目录，非进程推断）")
    ap.add_argument("--all", action="store_true", help="含终态 agent（默认只列 running）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--slug", help="按 SLUG 过滤")
    args = ap.parse_args()

    rows = [r for r in scan() if not is_aged_out(r)]
    if args.slug:
        rows = [r for r in rows if args.slug in (r["slug"] or r["worktree"])]
    if not args.all:
        rows = [r for r in rows if r["status"] == "running"]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return

    if not rows:
        scope = "全部" if args.all else "running"
        print(f"（{scope} 范围内无 agent 记录）")
        return

    if not args.all:
        print("RUNNING subagent（派发/重派前必查——无通知≠死亡，id1655；to 列=SendMessage 完整 id）：")
    hdr = f"{'TO (SendMessage)':44} {'STATUS':9} {'PROFILE':16} {'AGE':>6} {'SLUG/WORKTREE':28} DESC"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        tag = r["slug"] or r["worktree"] or "-"
        print(f"{r['agentId']:44} {r['status']:9} {r['profile']:16} {r['updatedAge']:>6} {tag:28} {r['description']}")


if __name__ == "__main__":
    sys.exit(main())
