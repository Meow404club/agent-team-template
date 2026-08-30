"""Brain built-in periodic incremental indexing (default: "project" source, 10 min).

复用 tool_refresh_index 的形态：每轮起一个独立索引子进程
（fresh process 吃当前 config.json/sources.json，崩溃不伤 brain，跑完内存即还）。
无改动时 mtime 缓存零嵌入调用——负载只在真有文件变化时产生，单轮秒级。

开关与节奏读 config.json 的 "autorefresh" 块（缺省即启用）：
  {"autorefresh": {"enabled": true, "interval_s": 600, "sources": ["project"], "startup_delay_s": 30}}
索引输出进 tmp/index/refresh.log（与手动 refresh 同一面），生命周期行进 autorefresh.log。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from brain import db

DEFAULTS = {"enabled": True, "interval_s": 600, "sources": ["project"],
            "startup_delay_s": 30, "run_timeout_s": 1800}


def _cfg() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg_path = Path(os.environ.get("BRAIN_RAG_CONFIG", str(db.TOOLS_DIR / "config.json")))
        with open(cfg_path, encoding="utf-8") as f:
            user = json.load(f).get("autorefresh", {})
        cfg.update({k: v for k, v in user.items()
                    if k in DEFAULTS and isinstance(v, type(DEFAULTS[k]))})
    except (OSError, json.JSONDecodeError, ValueError):
        pass  # 配置损坏按缺省跑
    return cfg


def _log(msg: str) -> None:
    line = f"[autorefresh {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(db.PROJECT_ROOT / "tmp" / "index" / "autorefresh.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _run_once(targets: list[str], timeout_s: int) -> None:
    # 与 server.tool_refresh_index 同款：子进程 stdout/stderr 追加进统一刷新日志
    log_path = db.PROJECT_ROOT / "tmp" / "index" / "refresh.log"
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"[autorefresh {time.strftime('%Y-%m-%d %H:%M:%S')}] refresh {targets}\n")
        log.flush()
        r = subprocess.run(
            [sys.executable, str(db.TOOLS_DIR / "brain" / "index.py"), *targets],
            cwd=str(db.TOOLS_DIR), stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            timeout=timeout_s,
        )
    if r.returncode != 0:
        _log(f"index exit={r.returncode} targets={targets}")


def _loop(cfg: dict) -> None:
    time.sleep(int(cfg["startup_delay_s"]))
    _log(f"loop start interval={cfg['interval_s']}s sources={cfg['sources']}")
    while True:
        t0 = time.time()
        try:
            _run_once(list(cfg["sources"]), int(cfg["run_timeout_s"]))
        except subprocess.TimeoutExpired:
            _log(f"run timed out after {cfg['run_timeout_s']}s")
        except Exception as e:  # 任何异常都不退出循环，brain 不受影响
            _log(f"ERROR {e!r}")
        _log(f"round done in {time.time() - t0:.1f}s")
        time.sleep(int(cfg["interval_s"]))


def start() -> threading.Thread | None:
    """brain 启动时调用；未启用或无 sources 返回 None，否则返回已启动的调度线程。"""
    cfg = _cfg()
    if not cfg.get("enabled") or not cfg.get("sources"):
        return None
    t = threading.Thread(target=_loop, args=(cfg,), name="brain-autorefresh", daemon=True)
    t.start()
    return t
