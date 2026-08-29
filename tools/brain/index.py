"""CLI indexer: python -m brain.index [source ...|all] [--force] [--limit-files N]"""
from __future__ import annotations

import argparse
import sys


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
