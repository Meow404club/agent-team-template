"""Search core shared by the MCP server.

Hybrid retrieval (Anthropic Contextual Retrieval recipe):
  semantic (MRL-truncated Qwen3 embeddings, cosine)  +  lexical (FTS5 bm25)
  fused with weighted Reciprocal Rank Fusion, then a light identifier-overlap
  boost. Top-20 fusion candidates are kept (paper: more fusion candidates help).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path

from brain import db
from brain.embed import embed_query

RG = "rg"
_RRF_K = 60
_W_VEC, _W_FTS = 0.7, 0.3


def _sources_subset(sources: list[str] | None) -> dict:
    all_sources = db.load_sources()
    if sources:
        unknown = [s for s in sources if s not in all_sources]
        if unknown:
            raise ValueError(f"未知资料源: {unknown}（可选: {', '.join(all_sources)}）")
        return {k: v for k, v in all_sources.items() if k in sources}
    return all_sources


def _fts_query(query: str) -> str:
    """Turn a free-form query into a tolerant FTS5 MATCH expression (ORs)."""
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}|\d+", query)
    toks = [t for t in toks if t.lower() not in ("the", "and", "for", "how", "what", "does", "with", "from")]
    if not toks:
        return '""'
    return " OR ".join('"%s"' % t.replace('"', '""') for t in toks[:24])


def _rerank(query: str, docs: list[str], timeout: int = 120) -> list[float] | None:
    """Call the local llama-server /v1/rerank endpoint (Qwen3-Reranker).

    Returns scores aligned with docs, or None if the service is unavailable
    (caller falls back to the fusion order).
    """
    import json as _json
    import urllib.request as _ur
    base = os.environ.get("BRAIN_RERANK_URL", "http://127.0.0.1:8938/v1")
    body = _json.dumps({"model": "reranker", "query": query, "documents": docs}).encode()
    req = _ur.Request(base.rstrip("/") + "/rerank", data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        opener = _ur.build_opener(_ur.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode())
        out = [0.0] * len(docs)
        for r in data.get("results", []):
            if 0 <= r["index"] < len(docs):
                out[r["index"]] = float(r["relevance_score"])
        return out
    except Exception:
        return None


def search_code(query: str, sources: list[str] | None = None, limit: int = 8,
                path_glob: str | None = None, candidate_k: int = 20,
                rerank: bool = True) -> list[dict]:
    subset = _sources_subset(sources)
    if not subset:
        return []
    con = db.connect()
    active = [s for s in subset if con.execute(
        "SELECT 1 FROM chunks WHERE source=? LIMIT 1", (s,)).fetchone()]
    if not active:
        con.close()
        return []

    qv = embed_query(query)
    fts_expr = _fts_query(query)

    # ---- semantic candidates (in-memory matrix per source; numpy fast path)
    vec_hits: dict[tuple, float] = {}
    try:
        import numpy as _np
        for src in active:
            rows = con.execute(
                "SELECT rowid, path, line, ord, vec FROM chunks WHERE source=?", (src,)
            ).fetchall()
            if not rows:
                continue
            dim = len(rows[0][4]) // 4
            mat = _np.empty((len(rows), dim), dtype=_np.float32)
            for i, r in enumerate(rows):
                mat[i] = _np.frombuffer(r[4], dtype=_np.float32)
            scores = mat @ _np.asarray(qv, dtype=_np.float32)
            for i, (rowid, path, line, ord_, _blob) in enumerate(rows):
                if path_glob and not (fnmatch.fnmatch(path, path_glob)
                                      or fnmatch.fnmatch(path.split("/")[-1], path_glob)):
                    continue
                if scores[i] > 0.15:
                    vec_hits[(src, path, ord_)] = float(scores[i])
    except ImportError:
        for src in active:
            for path, line, ord_, text, blob in con.execute(
                "SELECT path, line, ord, text, vec FROM chunks WHERE source=?", (src,)
            ):
                if path_glob and not (fnmatch.fnmatch(path, path_glob)
                                      or fnmatch.fnmatch(path.split("/")[-1], path_glob)):
                    continue
                score = db.cosine(qv, db.blob_to_vec(blob))
                if score > 0.15:
                    vec_hits[(src, path, ord_)] = score
    vec_rank = sorted(vec_hits.items(), key=lambda kv: -kv[1])

    # ---- lexical candidates (bm25 over contextualized body)
    fts_hits: dict[tuple, float] = {}
    for src in active:
        try:
            rows = con.execute(
                "SELECT path, ord, bm25(chunks_fts) FROM chunks_fts "
                "WHERE source=? AND chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                (src, fts_expr, candidate_k * 3),
            ).fetchall()
        except Exception:
            rows = []
        for path, ord_, rank in rows:
            if path_glob and not (fnmatch.fnmatch(path, path_glob)
                                  or fnmatch.fnmatch(path.split("/")[-1], path_glob)):
                continue
            fts_hits[(src, path, ord_)] = -rank  # bm25: smaller is better
    fts_rank = sorted(fts_hits.items(), key=lambda kv: -kv[1])

    # ---- weighted RRF fusion
    fused: dict[tuple, float] = {}
    for rank, (k, _s) in enumerate(vec_rank[:candidate_k]):
        fused[k] = fused.get(k, 0.0) + _W_VEC / (_RRF_K + rank + 1)
    for rank, (k, _s) in enumerate(fts_rank[:candidate_k]):
        fused[k] = fused.get(k, 0.0) + _W_FTS / (_RRF_K + rank + 1)

    q_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query.lower()))
    results: list[dict] = []
    for (src, path, ord_), rrf in sorted(fused.items(), key=lambda kv: -kv[1])[: limit * 3]:
        row = con.execute(
            "SELECT line, text, header FROM chunks WHERE source=? AND path=? AND ord=?",
            (src, path, ord_),
        ).fetchone()
        if not row:
            continue
        line, text, header = row
        # light identifier-overlap boost on the final ordering
        tl = text.lower()
        lex = sum(1 for t in q_tokens if t in tl) / max(1, len(q_tokens))
        results.append({
            "source": src,
            "file": f"{subset[src]['root']}/{path}",
            "line": line,
            "section": header,
            "score": round(rrf + 0.05 * lex, 5),
            "snippet": text[:1200],
            "_text": text,
        })
        if len(results) >= limit:
            break

    # ---- reranking (cross-encoder precision stage; see RESEARCH-NOTES)
    if rerank and len(results) > 1:
        scores = _rerank(query, [r["_text"][:2000] for r in results])
        if scores is not None:
            for r, s in zip(results, scores):
                r["rerank"] = round(s, 4)
                r["score"] = round(s, 4)  # cross-encoder verdict wins
            results.sort(key=lambda r: -r["score"])
    for r in results:
        r.pop("_text", None)
    con.close()
    return results


def sym_query(pattern: str, sources: list[str] | None = None,
              glob: str | None = None, limit: int = 40) -> list[dict]:
    """Exact regex search via ripgrep over the requested sources' roots."""
    subset = _sources_subset(sources)
    roots, seen = [], set()
    for cfg in subset.values():
        root = str((db.PROJECT_ROOT / cfg["root"]).resolve())
        if root not in seen and Path(root).exists():
            seen.add(root)
            roots.append(root)
    if not roots:
        return []
    cmd = [RG, pattern, "-n", "--no-heading", "-m", "3", "--glob", "!*.json", "--glob", "!*.png"]
    if glob:
        cmd += ["--glob", glob]
    cmd += roots[:8]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return [{"error": "ripgrep 超时"}]
    out = []
    for line in (proc.stdout or "").splitlines()[: limit * 3]:
        try:
            fp, ln, rest = line.split(":", 2)
        except ValueError:
            continue
        out.append({"file": fp, "line": int(ln), "text": rest.strip()[:300]})
        if len(out) >= limit:
            break
    return out


def get_source(file: str, start: int = 1, end: int | None = None) -> dict:
    """Read a file relative to the project root (used for tmp/ references too)."""
    p = (db.PROJECT_ROOT / file).resolve()
    if not str(p).startswith(str(db.PROJECT_ROOT.resolve())):
        return {"error": "路径越界"}
    if not p.is_file():
        return {"error": f"文件不存在: {file}"}
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return {"error": str(e)}
    end = end if end is not None else min(len(lines), start + 199)
    body = "\n".join(f"{i:5d}  {lines[i - 1]}" for i in range(max(1, start), min(len(lines), end) + 1))
    return {"file": file, "total_lines": len(lines), "range": [start, min(len(lines), end)], "content": body}


# --------------------------------------------------------------- mappings ---

_CLASS_RE = re.compile(r"^c\s+(\S+)\s+(?:(\S+)->|(\S+))")
_FIELD_RE = re.compile(r"^\s+f\s+(\S+)\s+(\S+)\s+(\S+)")
_METHOD_RE = re.compile(r"^\s+m\s+(\S+)\s+(\S+)\s+(\S+)")
_mappings_cache: tuple | None = None


def _load_mappings() -> tuple[dict, dict, dict]:
    global _mappings_cache
    if _mappings_cache is not None:
        return _mappings_cache
    path = Path(os.environ.get(
        "BRAIN_MAPPINGS_FILE", str(db.PROJECT_ROOT / "tmp" / "mappings" / "mappings.txt")))
    classes: dict[str, str] = {}
    fields: dict[str, str] = {}
    methods: dict[str, str] = {}
    if path.exists():
        current_obf = None
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _CLASS_RE.match(line)
                if m:
                    current_obf = m.group(1)
                    classes[current_obf] = m.group(2) or m.group(3) or ""
                    continue
                fm = _FIELD_RE.match(line)
                if fm and current_obf:
                    fields[f"{current_obf}/{fm.group(1)}"] = fm.group(3)
                    fields[fm.group(1)] = fm.group(3)
                    continue
                mm = _METHOD_RE.match(line)
                if mm and current_obf:
                    methods[f"{current_obf}/{mm.group(1)}"] = mm.group(3)
                    methods[mm.group(1)] = mm.group(3)
    _mappings_cache = (classes, fields, methods)
    return _mappings_cache


def mappings_lookup(term: str) -> dict:
    """Look up a codename in the mappings file (both directions).

    Optional feature: point BRAIN_MAPPINGS_FILE at a mappings txt (or drop one
    at tmp/mappings/mappings.txt). Missing file => empty results.
    """
    term = term.strip()
    classes, fields, methods = _load_mappings()
    out = {"term": term, "classes": {}, "fields": {}, "methods": {}}
    if term in classes or term in fields or term in methods:
        if term in classes:
            out["classes"][term] = classes[term]
        if term in fields:
            out["fields"][term] = fields[term]
        if term in methods:
            out["methods"][term] = methods[term]
        return out
    low = term.lower()
    for k, v in classes.items():
        if low in k.lower() or low in v.lower():
            out["classes"][k] = v
            if len(out["classes"]) >= 15:
                break
    for k, v in fields.items():
        if low in k.lower() or low in v.lower():
            out["fields"][k] = v
            if len(out["fields"]) >= 15:
                break
    for k, v in methods.items():
        if low in k.lower() or low in v.lower():
            out["methods"][k] = v
            if len(out["methods"]) >= 15:
                break
    return out


# ------------------------------------------------------------------- state --

def state_read(key: str | None = None, limit: int = 10) -> dict:
    """MemGPT 式分层读。无 key = 目录页：只返回各 key 的元信息（类型/条数/更新时间，
    按最近更新倒序），不返回内容——先浏览目录，再用 state_search 语义定位或按 key 取内容。
    带 key = 内容页：列表与记录型字典倒序（最新在前），超过 limit 只取最近；
    progress/architecture 等小结构型原样。读全量加大 limit。"""
    con = db.connect()
    import time as _t
    con.execute("DELETE FROM state_kv WHERE expires_at IS NOT NULL AND expires_at < ?",
                (_t.time(),))  # TTL 惰性清扫（LangGraph store 同款机会式）
    con.commit()
    if not key:
        out = {}
        for k, v, u in con.execute("SELECT key, value, updated FROM state_kv ORDER BY updated DESC"):
            try:
                data = json.loads(v)
            except json.JSONDecodeError:
                data = v
            if isinstance(data, dict):
                shape = f"dict/{len(data)}"
            elif isinstance(data, list):
                shape = f"list/{len(data)}"
            elif isinstance(data, str):
                shape = f"str/{len(data)}字"
            else:
                shape = type(data).__name__
            out[k] = {"updated": u, "shape": shape}
        con.close()
        return {"_usage": "目录页不含内容：state_read(key) 读单键（超长截断，limit 调大读全量）；"
                          "state_search(query) 语义定位；写入用 state_update",
                "keys": out}

    def shrink(v):
        if isinstance(v, list):
            out = list(reversed(v))  # append 型列表：尾部是最新
            return out[:limit] if len(out) > limit else out
        # 大账本才截（decisions/tasks/todo 等追加型 dict/list）：
        # progress/architecture 等小结构型不受影响，也不依赖条目形态猜测
        if isinstance(v, dict) and len(v) > limit:
            return dict(list(v.items())[-limit:][::-1])
        return v

    row = con.execute("SELECT value FROM state_kv WHERE key=?", (key,)).fetchone()
    con.close()
    if not row:
        return {key: None}
    try:
        return {key: shrink(json.loads(row[0]))}
    except json.JSONDecodeError:
        return {key: row[0]}
def _deep_merge(old, new):
    """深合并：dict+dict 递归（子典可只传变更字段，不清空既有卡片）；
    list+list 追加去重（commits 等增量登记幂等）；其余/类型不匹配新值整体覆盖。
    需要缩短/清空列表或整体替换时用 merge=false 覆盖式写。"""
    if isinstance(old, dict) and isinstance(new, dict):
        out = dict(old)
        for k, v in new.items():
            out[k] = _deep_merge(out[k], v) if k in out else v
        return out
    if isinstance(old, list) and isinstance(new, list):
        return old + [v for v in new if v not in old]
    return new


def state_update(key: str, value, merge: bool = False, ttl_seconds: float | None = None) -> dict:
    import time as _t
    # agent 经 MCP 传参时偶发把结构序列化成 JSON 字符串——先解回结构体，
    # 否则 json.dumps 双重编码、读回是 str 且 merge 的类型判断失效。
    # 解析失败（纯文本 todo/note 等）按原样存。
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass
    con = db.connect()
    if merge:
        row = con.execute("SELECT value FROM state_kv WHERE key=?", (key,)).fetchone()
        if row:
            try:
                old = json.loads(row[0])
            except json.JSONDecodeError:
                old = None
            if old is not None:
                value = _deep_merge(old, value)
    expires = (_t.time() + ttl_seconds) if ttl_seconds else None
    con.execute(
        "INSERT OR REPLACE INTO state_kv(key, value, updated, expires_at) VALUES(?,?,?,?)",
        (key, json.dumps(value, ensure_ascii=False), _t.time(), expires),
    )
    con.commit()
    con.close()
    try:  # 向量缓存尽力更新（失败不影响写入主路径）
        import sqlite3 as _s
        con2 = db.connect()
        _state_vec_sync(con2, key, json.dumps(value, ensure_ascii=False), _t.time())
        con2.close()
    except Exception:
        pass
    return {"ok": True, "key": key, "value": value}


# ---------------------------------------------------------------------- KG --
def kg_add(src: str, rel: str, dst: str, note: str = "",
           node_types: dict | None = None) -> dict:
    """写入一条关系。闸门：重复边忽略（幂等）；不允许既有的孤立节点当端点
    （防无意义悬挂节点增殖——先删孤立节点或换名字）；新节点允许（首条边会挂住它）。"""
    import time as _t
    src, rel, dst = src.strip(), rel.strip(), dst.strip()
    if not src or not rel or not dst:
        return {"ok": False, "error": "src/rel/dst 均不能为空"}
    con = db.connect()
    now = _t.time()
    try:
        # 孤儿拦截：节点存在但既无边也不是本次要建的端点
        for name in (src, dst):
            has_edge = con.execute(
                "SELECT 1 FROM kg_edges WHERE src=? OR dst=? LIMIT 1", (name, name)
            ).fetchone()
            exists = con.execute(
                "SELECT 1 FROM kg_nodes WHERE name=?", (name,)).fetchone()
            if exists and not has_edge:
                return {"ok": False,
                        "error": f"'{name}' 是已存在的孤立节点（无边）。先用 kg_del 删掉它，"
                                 f"或确认命名是否与既有节点重复（孤儿通常就是重复命名导致的）。"}
        dup = con.execute(
            "SELECT 1 FROM kg_edges WHERE src=? AND rel=? AND dst=?", (src, rel, dst)
        ).fetchone()
        if dup:
            return {"ok": True, "dedup": True, "edge": f"{src} -[{rel}]-> {dst}",
                    "note": "重复边已忽略（幂等）"}
        nt = node_types or {}
        for name in (src, dst):
            con.execute(
                "INSERT OR IGNORE INTO kg_nodes(name, type, note, created) VALUES(?,?,?,?)",
                (name, nt.get(name, "Entity"), "", now),
            )
        for name in (src, dst):
            if name in nt:
                con.execute("UPDATE kg_nodes SET type=? WHERE name=?", (nt[name], name))
        con.execute(
            "INSERT INTO kg_edges(src, rel, dst, note, created, valid_at) VALUES(?,?,?,?,?,?)",
            (src, rel, dst, note, now, now),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True, "edge": f"{src} -[{rel}]-> {dst}"}
def kg_query(entity: str | None = None, rel: str | None = None, limit: int = 30) -> dict:
    con = db.connect()
    q = "SELECT src, rel, dst, note FROM kg_edges WHERE 1=1"
    args: list = []
    if entity:
        q += " AND (src LIKE ? OR dst LIKE ?)"
        args += [f"%{entity}%", f"%{entity}%"]
    if rel:
        q += " AND rel LIKE ?"
        args += [f"%{rel}%"]
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    edges = [dict(zip(("src", "rel", "dst", "note"), r)) for r in con.execute(q, args)]
    nodes = [dict(zip(("name", "type", "note"), r))
             for r in con.execute("SELECT name, type, note FROM kg_nodes ORDER BY id DESC LIMIT ?", (limit,))]
    con.close()
    return {"edges": edges, "nodes": nodes}


def kg_del(entity: str) -> dict:
    con = db.connect()
    try:
        ids = [r[0] for r in con.execute(
            "SELECT id FROM kg_edges WHERE src=? OR dst=?", (entity, entity))]
        con.execute("DELETE FROM kg_edges WHERE src=? OR dst=?", (entity, entity))
        con.execute("DELETE FROM kg_nodes WHERE name=?", (entity,))
        if ids:
            con.executemany("DELETE FROM kg_vecs WHERE edge_id=?", [(i,) for i in ids])
        con.commit()
    finally:
        con.close()  # 异常路径也必须释放连接，否则写锁悬挂毒化整个进程
    return {"ok": True, "deleted": entity}


def state_search(query: str, k: int = 5, prefix: str | None = None, offset: int = 0) -> dict:
    """语义检索 state 账本：对每个 key 的值做向量缓存（写入时更新），cosine 召回
    top-k，返回 key+预览+分数。向量缺失/过期时惰性补嵌；嵌入服务不可用则降级为
    子串匹配。命中后用 state_read(key) 看全量。"""
    import math
    con = db.connect()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS state_vecs("
            "key TEXT PRIMARY KEY, updated REAL, text TEXT, vec BLOB)")
        import time as _t
        con.execute("DELETE FROM state_kv WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (_t.time(),))
        if prefix:
            rows = con.execute(
                "SELECT key, value, updated FROM state_kv WHERE key GLOB ?",
                (prefix.rstrip("*") + "*",)).fetchall()
        else:
            rows = con.execute("SELECT key, value, updated FROM state_kv").fetchall()
        cache = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
            "SELECT key, updated, text, vec FROM state_vecs")}
        # 惰性补齐过期/缺失向量（一次批量嵌入）
        stale = []
        for key, value, upd in rows:
            c = cache.get(key)
            if c is None or abs(c[0] - upd) > 0.5:
                stale.append((key, value, upd))
        if stale:
            try:
                from brain.embed import embed_texts
                texts = [json.dumps(v, ensure_ascii=False)[:2000] for _k, v, _u in stale]
                vecs = embed_texts(texts)
                for (key, _v, upd), text, vec in zip(stale, texts, vecs):
                    con.execute(
                        "INSERT OR REPLACE INTO state_vecs(key, updated, text, vec) VALUES(?,?,?,?)",
                        (key, upd, text, db.vec_to_blob(vec)))
                    cache[key] = (upd, text, db.vec_to_blob(vec))
                con.commit()
            except Exception:
                pass  # 嵌入不可用 → 走子串降级
        from brain.embed import embed_query
        qvec = embed_query(query)
        scored = []
        for key, value, upd in rows:
            c = cache.get(key)
            if c and c[2]:
                sim = db.cosine(qvec, db.blob_to_vec(c[2]))
            else:  # 子串降级
                hay = json.dumps(value, ensure_ascii=False)
                sim = 0.5 if query.lower() in hay.lower() else 0.0
            scored.append((sim, key, upd, value))
        scored.sort(reverse=True)
        out = []
        for sim, key, upd, value in scored[offset:offset + max(1, k)]:
            preview = json.dumps(value, ensure_ascii=False)[:220]
            out.append({"key": key, "score": round(sim, 4), "updated": upd,
                        "preview": preview, "read_full": f"state_read(key='{key}')"})
        return {"query": query, "results": out,
                "_usage": "按分数取用；内容全量用 state_read(key)"}
    finally:
        con.close()
def kg_invalidate(src: str, rel: str, dst: str, reason: str = "") -> dict:
    """关系过时的正规入口（双时态）：置 invalid_at 保留历史，演化链可查——
    不要用 kg_del 硬删（那会丢失"曾经成立过"的事实）。"""
    import time as _t
    con = db.connect()
    try:
        cur = con.execute(
            "UPDATE kg_edges SET invalid_at=? "
            "WHERE src=? AND rel=? AND dst=? AND invalid_at IS NULL",
            (_t.time(), src, rel, dst))
        con.commit()
    finally:
        con.close()
    if cur.rowcount == 0:
        return {"ok": False, "error": "未找到匹配的有效边（可能已失效或不存在），用 kg_query 确认"}
    return {"ok": True, "invalidated": cur.rowcount, "edge": f"{src} -[{rel}]-> {dst}",
            "reason": reason}
def kg_prune(dry_run: bool = True) -> dict:
    """清理孤儿节点（无任何边关联）。先跑 dry_run 看清单再动手。
    注意：根因多在重复命名——prune 前先检查孤儿名单里是否有该合并的同名变体。"""
    con = db.connect()
    try:
        orphans = [r[0] for r in con.execute(
            "SELECT name FROM kg_nodes n WHERE NOT EXISTS("
            "SELECT 1 FROM kg_edges e WHERE e.src=n.name OR e.dst=n.name)")]
        if dry_run:
            return {"ok": True, "orphans": orphans, "count": len(orphans),
                    "note": "dry_run；确认后 kg_prune(dry_run=false) 执行删除"}
        for name in orphans:
            con.execute("DELETE FROM kg_nodes WHERE name=?", (name,))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "removed": len(orphans), "names": orphans}
def kg_stats() -> dict:
    """KG 健康度：规模、类型分布、孤儿数、边类型 top。供定时整理与 project_status 引用。"""
    con = db.connect()
    try:
        return _kg_stats_impl(con)
    finally:
        con.close()
def project_status() -> dict:
    con = db.connect()
    per: dict = {}
    for s, f, c in con.execute(
        "SELECT f.source, COUNT(*), SUM(f.chunks) FROM files f GROUP BY f.source"
    ):
        per[s] = {"files": f, "chunks": c or 0}
    kg_e = con.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]
    state = {k: u for k, u in con.execute("SELECT key, updated FROM state_kv")}
    con.close()
    import subprocess as _sp
    wt = _sp.run(["git", "worktree", "list"], capture_output=True, text=True,
                 cwd=str(db.PROJECT_ROOT)).stdout.strip().splitlines()
    log = _sp.run(["git", "log", "--oneline", "-8"], capture_output=True, text=True,
                  cwd=str(db.PROJECT_ROOT)).stdout.strip()
    return {"index": per, "kg_edges": kg_e, "state_keys": state,
            "worktrees": wt, "recent_commits": log}
