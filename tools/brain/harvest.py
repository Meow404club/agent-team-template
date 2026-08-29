"""Harvest: pull reference material to tmp/harvest/ for repeated consultation.

Researcher-facing tool. Deliberately does NOT touch the index:
- harvested files are immediately readable via get_source / sym_query
  (both hit the filesystem directly);
- whether a harvest enters the RAG is the orchestrator's decision
  (refresh_index with source="harvest" — incremental by mtime/size);
- a meta.json per harvest gives the orchestrator enough context to decide
  (url, final url, kind, file count, bytes, date).

kinds:
  page — HTML converted to Markdown (html2text), saved as <name>/index.md
  file — saved verbatim as <name>/<url basename>
  repo — tar.gz (GitHub codeload or any .tar.gz/.tgz) unpacked into <name>/,
         top-level distdir stripped; path-traversal-safe
  auto — repo if URL looks like a tarball, else page/file by content-type
"""
from __future__ import annotations

import json
import os
import re
import tarfile
import tempfile
import time
from pathlib import Path

from brain import db

HARVEST_ROOT = db.PROJECT_ROOT / "tmp" / "harvest"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
TARBALL_RE = re.compile(r"\.(tar\.gz|tgz)([?#]|$)")
MAX_PAGE_BYTES = 10 * 1024 * 1024
MAX_TARBALL_BYTES = 200 * 1024 * 1024
MAX_UNPACK_FILES = 20000
MAX_UNPACK_BYTES = 512 * 1024 * 1024

_NO_INDEX_NOTE = (
    "未入 RAG（不阻塞、不自动索引）。文件已可立即用 get_source(file='tmp/harvest/...') "
    "与 sym_query(sources=['harvest']) 阅读；是否索引由主 agent 决策 "
    "（refresh_index(source='harvest')，按 mtime 增量只嵌新文件）。"
)


def _safe_name(name: str) -> str:
    name = (name or "").strip().lower().replace(" ", "-")
    if not NAME_RE.match(name):
        raise ValueError(f"name 须匹配 {NAME_RE.pattern}（kebab-case，防路径穿越）: {name!r}")
    return name


def _check_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError(f"仅支持 http(s) URL: {url!r}")
    return url


def _get(url: str, timeout: int, max_bytes: int):
    from curl_cffi import requests as cffi  # 延迟导入，统一报错文案
    resp = cffi.get(url, impersonate="chrome", timeout=timeout,
                    allow_redirects=True,
                    headers={"Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"})
    if len(resp.content) > max_bytes:
        raise ValueError(f"响应超过上限 {max_bytes} 字节（{len(resp.content)}），"
                         f"超大资料请走任务卡由主 agent 处理")
    return resp


def _html_to_md(html: str, base_url: str) -> str:
    import html2text
    h = html2text.HTML2Text(baseurl=base_url)
    h.body_width = 0          # 不硬折行，保留语义段落便于 md 分块
    h.ignore_images = False
    h.single_line_break = False
    return h.handle(html)


def _common_top(members: list) -> str:
    tops = {m.name.split("/", 1)[0] for m in members if m.name}
    return tops.pop() if len(tops) == 1 else ""


def _unpack_tarball(data: bytes, dest: Path) -> tuple[int, int]:
    """Path-traversal-safe unpack; strips a single common top-level distdir."""
    dest_res = str(dest.resolve()) + os.sep
    n_files = n_bytes = 0
    with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as fh:
        fh.write(data)
        fh.seek(0)
        with tarfile.open(fileobj=fh, mode="r:gz") as tf:
            members = tf.getmembers()
            top = _common_top(members)
            for m in members:
                if not m.isreg():      # 只收常规文件：目录自动 mkdir，链接/设备一律跳过
                    continue
                rel = m.name
                if top and rel.startswith(top + "/"):
                    rel = rel[len(top) + 1:]
                if not rel or rel.startswith(".git/"):
                    continue
                target = (dest / rel).resolve()
                if not str(target).startswith(dest_res):
                    raise ValueError(f"tar 成员越界，已中止: {m.name!r}")
                if n_files >= MAX_UNPACK_FILES:
                    raise ValueError(f"解包文件数超过上限 {MAX_UNPACK_FILES}")
                n_bytes += m.size
                if n_bytes > MAX_UNPACK_BYTES:
                    raise ValueError(f"解包总量超过上限 {MAX_UNPACK_BYTES}")
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is not None:
                    target.write_bytes(src.read())
                    n_files += 1
    return n_files, n_bytes


def _count_files(dest: Path) -> tuple[int, int]:
    n = size = 0
    for p in dest.rglob("*"):
        if p.is_file():
            n += 1
            size += p.stat().st_size
    return n, size


def harvest(url: str, name: str, kind: str = "auto",
            raw: bool = False, timeout: int = 30) -> dict:
    """落盘外部参考资料到 tmp/harvest/<name>/。绝不触碰索引。"""
    url = _check_url(url)
    name = _safe_name(name)
    if kind not in ("auto", "page", "file", "repo"):
        raise ValueError(f"kind 须为 auto|page|file|repo: {kind!r}")
    if kind == "auto" and ("codeload.github.com" in url or TARBALL_RE.search(url)):
        kind = "repo"

    dest = HARVEST_ROOT / name
    dest.mkdir(parents=True, exist_ok=True)

    if kind == "repo":
        resp = _get(url, timeout, MAX_TARBALL_BYTES)
        n_files, n_bytes = _unpack_tarball(resp.content, dest)
        kind_used = "repo"
    else:
        resp = _get(url, timeout, MAX_PAGE_BYTES)
        ctype = (resp.headers.get("content-type") or "").lower()
        if kind == "auto":
            kind = "page" if ("html" in ctype and not raw) else "file"
        if kind == "page":
            md = _html_to_md(resp.text, str(resp.url))
            (dest / "index.md").write_text(md, encoding="utf-8")
            n_files, n_bytes = 1, len(md.encode())
        else:
            base = url.split("#")[0].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            fname = base if re.match(r"^[\w.\-]{1,120}$", base) else "file.bin"
            (dest / fname).write_bytes(resp.content)
            n_files, n_bytes = 1, len(resp.content)
        kind_used = kind

    total_files, total_bytes = _count_files(dest)
    meta = {"url": url, "final_url": str(resp.url), "kind": kind_used,
            "status": resp.status_code, "files": total_files,
            "bytes": total_bytes, "date": time.strftime("%Y-%m-%d %H:%M")}
    (dest / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    return {"ok": True, "name": name, "kind": kind_used, **meta,
            "root": f"tmp/harvest/{name}", "note": _NO_INDEX_NOTE}
