"""Indexer CLI: tools/.venv/bin/python tools/brain/index.py [source ...|all] [--force] [--limit-files N]

Self-bootstraps sys.path, so it runs from any cwd (repo root, tools/, elsewhere).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))


def main() -> None:
    from brain import db

    ap = argparse.ArgumentParser(prog="brain.index")
    ap.add_argument("targets", nargs="*", default=["all"])
    ap.add_argument("--force", action="store_true", help="忽略 mtime 缓存强制重嵌")
    ap.add_argument("--limit-files", type=int, default=None, help="每个源最多索引 N 个文件（试跑用）")
    args = ap.parse_args()

    sources = db.load_sources()
    targets = args.targets or ["all"]
    if targets == ["all"]:
        targets = list(sources.keys())
    db.index_targets(targets, args.limit_files, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
