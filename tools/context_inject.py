#!/usr/bin/env python3
"""SessionStart hook：向新会话注入压缩状态页（自动上下文恢复）。

工程卫生纪律（源自 ruflo ADR-050/060/095 教训）：
- 恒 exit 0，任何异常静默退出——hook 故障绝不能阻塞会话启动
- 无副作用：SQLite 只读模式 + 不写任何文件
- 作用域自探测：cwd 所在仓库根含 tools/brain/server.py 才注入（可挂用户级免信任审核）
- 输出 ≤40 行（~500 token 预算）
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def repo_root() -> Path | None:
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        root = Path(root) if root else None
    except Exception:
        return None
    if root and ((root / "tools" / "brain" / "server.py").exists()
                 or (root / "tools" / "gt6_rag" / "server.py").exists()):
        return root
    return None


def git_line(root: Path, args: list[str]) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


def main() -> None:
    root = repo_root()
    if root is None:
        return
    db = root / "tmp" / "index" / "rag.db"
    if not db.exists():
        print("(brain 索引未建立：tools/.venv/bin/python tools/brain/index.py all)")
        return
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row

        def val(key: str):
            row = con.execute("SELECT value FROM state_kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

        out: list[str] = []
        prog = val("progress") or {}
        out.append(f"◆ 阶段: {prog.get('phase', '?')}｜当前: {prog.get('current', '?')}｜"
                   f"下一步: {prog.get('next', '?')}")
        tasks = [r[0] for r in con.execute(
            "SELECT key FROM state_kv WHERE key LIKE 'tasks.%' ORDER BY updated DESC LIMIT 8")]
        if tasks:
            out.append("◆ 在途任务键: " + ", ".join(t.split(".", 1)[1] for t in tasks))
        decisions = val("decisions") or {}
        if isinstance(decisions, dict) and decisions:
            items = list(decisions.items())[-3:][::-1]
            for k, v in items[:3]:
                topic = v.get("topic", k) if isinstance(v, dict) else str(v)[:40]
                out.append(f"◆ 近决策: {topic}")
        bugs = val("known_bugs")
        if isinstance(bugs, list):
            open_n = sum(1 for b in bugs if isinstance(b, dict) and b.get("status") != "fixed")
            out.append(f"◆ 已知 Bug: {len(bugs)} 条（未闭 {open_n}）")
        con.close()
    except Exception:
        out = ["◆ (brain 状态读取失败——服务或索引未就绪，不影响会话)"]

    wt = git_line(root, ["worktree", "list"])
    n_wt = len(wt.splitlines()) if wt else 0
    last = git_line(root, ["log", "--oneline", "-1"])
    if last:
        out.append(f"◆ main 最新: {last[:80]}｜worktree {n_wt} 个")
    out.append("◆ 说『恢复上下文』取全量装载；直接下达任务即从断点继续。")
    print("\n".join(out[:40]))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)  # 恒 0：hook 故障不阻塞会话
