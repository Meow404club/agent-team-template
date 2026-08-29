"""Semantic memory layer (mem0-inspired) on top of the same rag.db.

- `remember(kind, text)`: ADD a memory; if a very similar active memory exists,
  UPDATE it in place (mem0-style consolidation) instead of duplicating.
- `recall(query, k)`: cosine retrieval over memory vectors, active-first.
- `forget(memory_id)`: mark inactive (soft delete, keeps audit trail).
- KG edges are also embedded: kg_add embeds `src rel dst note` so
  `kg_search(query)` gives semantic access to the graph.
"""
from __future__ import annotations

import json
import time

from brain import db
from brain.embed import embed_texts, embed_query


def _ensure_tables(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT, text TEXT, vec BLOB, active INTEGER DEFAULT 1,
          hits INTEGER DEFAULT 0, created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS kg_vecs(
          edge_id INTEGER PRIMARY KEY, vec BLOB, created REAL);
        """
    )
    con.commit()


def remember(kind: str, text: str) -> dict:
    """Store a memory. Consolidates: near-duplicate of an active memory updates it."""
    if not text or not text.strip():
        return {"error": "empty text"}
    text = text.strip()[:4000]
    con = db.connect()
    _ensure_tables(con)
    now = time.time()
    vec = embed_texts([text])[0]

    # consolidation: find the most similar active memory of any kind
    best_id, best_sim = None, 0.0
    for mid, blob in con.execute("SELECT id, vec FROM memories WHERE active=1"):
        sim = db.cosine(vec, db.blob_to_vec(blob))
        if sim > best_sim:
            best_id, best_sim = mid, sim
    if best_sim >= 0.97 and best_id is not None:
        # same fact: refresh timestamp only (NOOP-ish)
        con.execute("UPDATE memories SET updated=?, hits=hits WHERE id=?", (now, best_id))
        con.commit()
        con.close()
        return {"ok": True, "action": "noop_duplicate", "id": best_id, "sim": round(best_sim, 3)}
    if best_sim >= 0.80 and best_id is not None:
        # complementary info: merge into existing memory (UPDATE)
        row = con.execute("SELECT text FROM memories WHERE id=?", (best_id,)).fetchone()
        merged = (row[0] + "\n[update] " + text)[:4000]
        mvec = embed_texts([merged])[0]
        con.execute("UPDATE memories SET text=?, vec=?, updated=? WHERE id=?",
                    (merged, db.vec_to_blob(mvec), now, best_id))
        con.commit()
        con.close()
        return {"ok": True, "action": "updated", "id": best_id, "sim": round(best_sim, 3)}
    cur = con.execute(
        "INSERT INTO memories(kind, text, vec, active, created, updated) VALUES(?,?,?,1,?,?)",
        (kind, text, db.vec_to_blob(vec), now, now),
    )
    con.commit()
    mid = cur.lastrowid
    con.close()
    return {"ok": True, "action": "added", "id": mid}


def recall(query: str, k: int = 8, kind: str | None = None) -> list[dict]:
    """Semantic recall over memories (active first, then similarity)."""
    if not query.strip():
        return []
    qv = embed_query(query)
    con = db.connect()
    _ensure_tables(con)
    sql = "SELECT id, kind, text, vec, active, hits, updated FROM memories"
    args: list = []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    rows = con.execute(sql, args).fetchall()
    con.execute("UPDATE memories SET hits = hits + 1 WHERE active=1")  # cheap recency signal
    con.commit()
    con.close()
    scored = []
    for mid, kd, text, blob, active, hits, updated in rows:
        sim = db.cosine(qv, db.blob_to_vec(blob))
        # stale memories fade a little (30-day half-life-ish), inactive sink hard
        age_days = max(0.0, (time.time() - (updated or 0)) / 86400)
        decay = 0.5 ** (age_days / 30.0)
        final = sim * (0.6 + 0.4 * decay) * (1.0 if active else 0.25)
        scored.append({"id": mid, "kind": kd, "active": bool(active), "sim": round(sim, 3),
                       "text": text, "updated": updated, "_final": round(final, 4)})
    scored.sort(key=lambda m: -m["_final"])
    return [{kk: vv for kk, vv in m.items() if kk != "_final"} for m in scored[:k]]


def forget(memory_id: int) -> dict:
    con = db.connect()
    _ensure_tables(con)
    con.execute("UPDATE memories SET active=0 WHERE id=?", (memory_id,))
    con.commit()
    con.close()
    return {"ok": True, "forgotten": memory_id}


# ------------------------------------------------------- KG semantic mirror --

def kg_add_embedded(src: str, rel: str, dst: str, note: str = "",
                    node_types: dict | None = None) -> dict:
    """kg_add + embed the triple so kg_search can find it semantically."""
    res = db  # noqa: F841  (documentation hook)
    from brain.search import kg_add
    added = kg_add(src, rel, dst, note, node_types)
    con = db.connect()
    _ensure_tables(con)
    text = f"{src} -[{rel}]-> {dst}" + (f" | {note}" if note else "")
    edge = con.execute(
        "SELECT id FROM kg_edges WHERE src=? AND rel=? AND dst=?", (src, rel, dst)
    ).fetchone()
    if edge:
        vec = embed_texts([text])[0]
        con.execute("INSERT OR REPLACE INTO kg_vecs(edge_id, vec, created) VALUES(?,?,?)",
                    (edge[0], db.vec_to_blob(vec), time.time()))
    con.commit()
    con.close()
    return added


def kg_search(query: str, k: int = 10) -> list[dict]:
    """Semantic search over KG triples."""
    if not query.strip():
        return []
    qv = embed_query(query)
    con = db.connect()
    _ensure_tables(con)
    rows = con.execute(
        "SELECT e.id, e.src, e.rel, e.dst, e.note, v.vec FROM kg_edges e "
        "LEFT JOIN kg_vecs v ON v.edge_id = e.id"
    ).fetchall()
    con.close()
    out = []
    for eid, src, rel, dst, note, blob in rows:
        sim = db.cosine(qv, db.blob_to_vec(blob)) if blob else 0.0
        out.append({"id": eid, "triple": f"{src} -[{rel}]-> {dst}",
                    "note": note, "sim": round(sim, 3)})
    out.sort(key=lambda x: -x["sim"])
    return out[:k]
