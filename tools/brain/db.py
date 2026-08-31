"""SQLite vector store + index builder (rag.db).

Hybrid retrieval per Anthropic "Contextual Retrieval" (2024-09):
- embeddings on contextualized chunks (chunk text prefixed with a short
  "which file/class/section is this from" header, no LLM needed for code);
- BM25 via SQLite FTS5 over the same contextualized text;
- scores combined with weighted Reciprocal Rank Fusion.

Schema:
  files(source, path, mtime, size, chunks)
  chunks(source, path, line, ord, header, text, vec BLOB)
  chunks_fts(source, path, ord, body)        -- FTS5 bm25 mirror
  kg_nodes / kg_edges / state_kv
"""
from __future__ import annotations

import fnmatch
import json
import math
import os
import sqlite3
import struct
import time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = TOOLS_DIR.parent
DB_PATH = PROJECT_ROOT / "tmp" / "index" / "rag.db"
SOURCES_PATH = TOOLS_DIR / "sources.json"

os.environ.setdefault("GT6_RAG_DB", str(DB_PATH))

RRF_K = 60


def db_path() -> Path:
    return Path(os.environ.get("GT6_RAG_DB", str(DB_PATH)))


def connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(con)
    _migrate(con)
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS files(
          source TEXT NOT NULL, path TEXT NOT NULL,
          mtime REAL, size INTEGER, chunks INTEGER,
          PRIMARY KEY(source, path));
        CREATE TABLE IF NOT EXISTS chunks(
          source TEXT NOT NULL, path TEXT NOT NULL, line INTEGER,
          ord INTEGER, header TEXT, text TEXT, vec BLOB,
          PRIMARY KEY(source, path, ord));
        CREATE TABLE IF NOT EXISTS chunks_fts(
          source, path, ord, body,
          PRIMARY KEY(source, path, ord)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS kg_nodes(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE, type TEXT, note TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS kg_edges(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          src TEXT, rel TEXT, dst TEXT, note TEXT, created REAL,
          UNIQUE(src, rel, dst));
        CREATE TABLE IF NOT EXISTS state_kv(key TEXT PRIMARY KEY, value TEXT, updated REAL);
        """
    )
    con.commit()


# ---------------------------------------------------------------- sources ---

def _migrate(con: sqlite3.Connection) -> None:
    """增量列迁移（SQLite 无 ADD COLUMN IF NOT EXISTS，逐条 try）。
    2026-09-01 记忆治理研究卡：memories supersede 演化链 / KG 双时态 / state TTL。"""
    for stmt in (
        "ALTER TABLE memories ADD COLUMN supersedes_id INTEGER",
        "ALTER TABLE memories ADD COLUMN invalid_at REAL",
        "ALTER TABLE kg_edges ADD COLUMN valid_at REAL",
        "ALTER TABLE kg_edges ADD COLUMN invalid_at REAL",
        "ALTER TABLE state_kv ADD COLUMN expires_at REAL",
    ):
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass  # 列已存在
    con.commit()



def load_sources() -> dict:
    with open(SOURCES_PATH, encoding="utf-8") as f:
        sources = json.load(f)
    # curator 的运行时裁剪规则（tmp 区，不入库、改完即时生效，无需重启 brain）：
    # tmp/harvest/exclude.json = ["<相对 tmp/harvest 的 glob>", ...]
    extra = PROJECT_ROOT / "tmp" / "harvest" / "exclude.json"
    if "harvest" in sources and extra.exists():
        try:
            with open(extra, encoding="utf-8") as f:
                globs = json.load(f)
            if isinstance(globs, list):
                merged = list(sources["harvest"].get("exclude", []))
                merged.extend(g for g in globs if isinstance(g, str))
                sources["harvest"]["exclude"] = merged
        except (OSError, json.JSONDecodeError):
            pass  # 规则文件损坏时按无规则索引，curator 会发现并修复
    return sources


def _match_any(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def iter_source_files(source: str, cfg: dict, limit_files: int | None = None):
    root = (PROJECT_ROOT / cfg["root"]).resolve()
    if not root.exists():
        return
    inc = cfg.get("include", ["**/*"])
    exc = cfg.get("exclude", [])
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in (".git", "node_modules", "build", ".gradle", "target"))
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            rel_root = full.relative_to(root).as_posix()
            rel_proj = full.relative_to(PROJECT_ROOT).as_posix()
            if _match_any(rel_root, inc) and not _match_any(rel_root, exc) \
               and not _match_any(rel_proj, exc):
                n += 1
                if limit_files and n > limit_files:
                    return
                yield root, full


# ------------------------------------------------------------- vec utils ----

def vec_to_blob(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def blob_to_vec(b: bytes) -> list[float]:
    n = len(b) // 4
    return list(struct.unpack(f"{n}f", b))


def cosine(a: list[float], b: list[float]) -> float:
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


# -------------------------------------------------------- contextualization --

def contextual_prefix(source: str, rel_path: str, header: str, lang: str) -> str:
    """Short header situating the chunk (Anthropic contextual-retrieval style).

    Returns a 1-2 line plain-text prefix; embedded together with the chunk and
    fed to BM25. For code we derive it cheaply from path/class metadata instead
    of asking an LLM to write it.
    """
    if lang == "java":
        pkg = rel_path.rsplit("/", 1)[0].replace("/", ".")
        h = header or rel_path
        return f"[{source}] file {rel_path} (package {pkg}); chunk: {h}"
    if lang == "md":
        return f"[{source}] doc {rel_path} — section: {header}"
    return f"[{source}] {rel_path} — {header}"


# ---------------------------------------------------------------- indexer ---

def index_source(con: sqlite3.Connection, source: str, cfg: dict,
                 limit_files: int | None = None, force: bool = False,
                 log=print) -> tuple[int, int]:
    """Index one source; returns (files_indexed, chunks_indexed)."""
    from brain.chunking import chunk_file, resolve_lang
    from brain.embed import embed_texts

    lang_cfg = cfg.get("lang", "text")
    done_files = done_chunks = 0
    batch: list[dict] = []
    batch_meta: dict = {}

    def flush():
        nonlocal done_files, done_chunks
        if not batch:
            return
        # contextual retrieval: embed & index the header+chunk composite
        bodies = [b["context"] + "\n" + b["text"] for b in batch]
        vecs = embed_texts(bodies) if bodies else []
        for i, b in enumerate(batch):
            con.execute(
                "INSERT OR REPLACE INTO chunks(source, path, line, ord, header, text, vec) VALUES(?,?,?,?,?,?,?)",
                (source, b["rel"], b["line"], b["ord"], b["header"], b["text"], vec_to_blob(vecs[i])),
            )
            con.execute(
                "INSERT OR REPLACE INTO chunks_fts(source, path, ord, body) VALUES(?,?,?,?)",
                (source, b["rel"], b["ord"], b["context"] + "\n" + b["text"]),
            )
        for rel, meta in batch_meta.items():
            con.execute(
                "INSERT OR REPLACE INTO files(source, path, mtime, size, chunks) VALUES(?,?,?,?,?)",
                (source, rel, meta["mtime"], meta["size"], meta["nchunks"]),
            )
        done_files += len(batch_meta)
        done_chunks += len(batch)
        con.commit()
        batch.clear()
        batch_meta.clear()

    skipped = 0
    for root, full in iter_source_files(source, cfg, limit_files):
        rel = full.relative_to(root).as_posix()
        try:
            st = full.stat()
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > 400_000:
            skipped += 1
            continue
        row = con.execute(
            "SELECT mtime, size FROM files WHERE source=? AND path=?", (source, rel)
        ).fetchone()
        if not force and row and abs(row[0] - st.st_mtime) < 1 and row[1] == st.st_size:
            skipped += 1
            continue
        # lang=auto：逐文件按扩展名解析（harvest 等混合资料源）
        lang = resolve_lang(rel, "text") if lang_cfg == "auto" else lang_cfg
        chunks = chunk_file(lang, text, rel)
        if not chunks:
            continue
        for ord_, chunk in enumerate(chunks):
            line = chunk["line"]
            header = chunk["header"]
            ctext = chunk["text"]
            batch.append({
                "rel": rel, "line": line, "ord": ord_, "header": header,
                "text": ctext,
                "context": contextual_prefix(source, rel, header, lang),
            })
        batch_meta[rel] = {"mtime": st.st_mtime, "size": st.st_size, "nchunks": len(chunks)}
        if len(batch) >= 512:
            flush()
            log(f"  [{source}] {done_files} files / {done_chunks} chunks ...")
    flush()
    if limit_files is None:
        known = {r[0] for r in con.execute("SELECT path FROM files WHERE source=?", (source,))}
        seen = set()
        for root, full in iter_source_files(source, cfg):
            seen.add(full.relative_to(root).as_posix())
        for gone in known - seen:
            con.execute("DELETE FROM chunks WHERE source=? AND path=?", (source, gone))
            con.execute("DELETE FROM chunks_fts WHERE source=? AND path=?", (source, gone))
            con.execute("DELETE FROM files WHERE source=? AND path=?", (source, gone))
        con.commit()
    if skipped:
        log(f"  [{source}] 未变化跳过 {skipped} 个文件")
    return done_files, done_chunks


def index_targets(targets: list[str], limit_files: int | None, force: bool, log=print) -> None:
    sources = load_sources()
    con = connect()
    t0 = time.time()
    for name in targets:
        if name not in sources:
            log(f"未知资料源: {name}（可选: {', '.join(sources)}）")
            continue
        log(f"索引 {name}: {sources[name].get('desc', '')}")
        f, c = index_source(con, name, sources[name], limit_files, force, log)
        log(f"  [{name}] 完成: {f} 文件, {c} 块")
    total_f = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    total_c = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.close()
    log(f"总计 {total_f} 文件 / {total_c} 块，用时 {time.time() - t0:.0f}s")
