"""Embedding client.

Backend = any OpenAI-compatible /embeddings endpoint, selected by
`base_url`/`api_key`/`model` in tools/config.json:
  - local:  llama-server (llama.cpp) with --embedding, see tools/embed_server.sh.
            Fast, free, deterministic; returns L2-normalized vectors
            (--embd-normalize 2), so cosine similarity == dot product.
  - remote: any hosted OpenAI-compatible relay/API.

MRL truncation (client-side head slicing + renormalize) is applied per config
`truncate_dims` (0 = keep native dimensions).
"""
from __future__ import annotations

import json
import math
import os
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = TOOLS_DIR / "config.json"

# brain 服务器（server._inflight_write）写的在途正常请求计数；索引侧让位用
INFLIGHT_PATH = TOOLS_DIR.parent / "tmp" / "index" / "inflight.json"
INFLIGHT_STALE_S = 900.0  # 计数泄漏兜底：文件超 15 分钟未更新视作无人在途
                          # （server 启动即复位，这里只兜 kill -9 后残留）

_config: dict | None = None
# 直连本地服务必须绕过系统代理（本机曾因 http_proxy 环境吃 502）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _inflight_count() -> int:
    try:
        with open(INFLIGHT_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if time.time() - float(d.get("ts", 0)) > INFLIGHT_STALE_S:
            return 0
        return max(0, int(d.get("n", 0)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0  # 文件缺失/损坏 = 无让位需求，照常工作


def _yield_to_interactive(poll_s: float = 1.5) -> None:
    """索引嵌入让位：brain 有在途正常请求时等待，归零/过期/文件消失才继续。
    只在 polite 路径（索引构建）调用；交互请求自身的嵌入绝不走这里。"""
    waited = False
    t0 = time.time()
    while _inflight_count() > 0:
        if not waited:
            print("[embed] brain 交互请求在途，索引嵌入让位 ...", flush=True)
            waited = True
        time.sleep(poll_s)
    if waited:
        print(f"[embed] 让位结束（等待 {time.time() - t0:.1f}s），继续索引嵌入", flush=True)


def load_config() -> dict:
    global _config
    if _config is None:
        cfg_path = Path(os.environ.get("BRAIN_RAG_CONFIG", CONFIG_PATH))
        with open(cfg_path, encoding="utf-8") as f:
            _config = json.load(f)["embedding"]
    return _config


class _ExceedContextError(RuntimeError):
    """单请求实际 token 超过 llama-server 槽 n_ctx（HTTP 400 exceed_context_size_error）。

    确定性失败，重试无意义——必须切块降级。est 公式对代码/符号密集文本低估约 2 倍，
    不能用 est 预判，只能按响应体定性后处理（2026-09-20 P32 事故根因）。"""


def _post_batch(texts: list[str], retries: int | None = None) -> list[list[float]]:
    cfg = load_config()
    payload = json.dumps({"model": cfg["model"], "input": texts}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    url = cfg["base_url"].rstrip("/") + "/embeddings"
    # 交互式小请求（remember/recall/search_code）必须把总预算压在 MCP 客户端
    # 超时（60s）之内：共享的 GPU 服务饱和时，长超时×多重试会让调用隐形挂死。
    # 大批量（索引构建）没有客户端在等，保留长超时与多次重试。
    est = _token_estimate(texts)
    if est < 2000:
        timeout = int(cfg.get("interactive_timeout_s", 15))
        if retries is None:
            retries = int(cfg.get("interactive_retries", 1))
    else:
        timeout = int(cfg.get("batch_timeout_s", 240))
        if retries is None:
            retries = 7
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with _opener.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 400 and "exceed_context_size_error" in body:
                raise _ExceedContextError(body) from e
            last_err = e
            time.sleep(min(60, (2 ** attempt) * 2) + random.random() * 2)
        except (urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError, KeyError) as e:
            last_err = e
            time.sleep(min(60, (2 ** attempt) * 2) + random.random() * 2)
    raise RuntimeError(
        f"embedding request failed after {retries} retries "
        f"(url={url}, timeout={timeout}s, items={len(texts)}, est_tokens={est}): {last_err}")


_FIT_CHUNK_CHARS = 2500  # 起始块长：混合文本 actual ≈ est×2，2500 字符块实测可过 2048 槽


def _embed_chunk_vecs(text: str) -> list[list[float]]:
    """超限单条切块嵌入，返回各块向量。块仍超槽则递归对半（est 对代码密集文本
    低估，无法预判安全块长，只能以 400 响应为信号收缩）。"""
    chunks = [text[i:i + _FIT_CHUNK_CHARS] for i in range(0, len(text), _FIT_CHUNK_CHARS)] or [text]
    vecs: list[list[float]] = []
    for c in chunks:
        try:
            vecs.extend(_post_batch([c]))
        except _ExceedContextError:
            if len(c) <= 200:
                raise
            half = len(c) // 2
            vecs.extend(_embed_chunk_vecs(c[:half]))
            vecs.extend(_embed_chunk_vecs(c[half:]))
    return vecs


def _mean_vector(vecs: list[list[float]]) -> list[float]:
    n = len(vecs)
    v = [sum(col) / n for col in zip(*vecs)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _post_batch_fit(texts: list[str]) -> list[list[float]]:
    """_post_batch 的超限降级壳：批超限→拆条；条超限→切块嵌入后向量均值合并
    （单向量表示整条，尾部内容以块向量并集近似保召回）。"""
    try:
        return _post_batch(texts)
    except _ExceedContextError:
        if len(texts) == 1:
            return [_mean_vector(_embed_chunk_vecs(texts[0]))]
        out: list[list[float]] = []
        for t in texts:
            out.extend(_post_batch_fit([t]))
        return out


def _truncate(v: list[float], dims: int) -> list[float]:
    if dims <= 0 or len(v) <= dims:
        return v
    head = v[:dims]
    norm = math.sqrt(sum(x * x for x in head)) or 1.0
    return [x / norm for x in head]


def _token_estimate(texts: list[str]) -> int:
    """Rough token estimate: ~4 chars/token for code+EN, CJK ~1.5 chars/token.

    Conservative overcount is fine (smaller batches, no failures).
    """
    import unicodedata
    cjk = sum(1 for t in texts for ch in t if unicodedata.east_asian_width(ch) in "WF")
    total_chars = sum(len(t) for t in texts)
    return int((total_chars - cjk) / 4 + cjk / 1.5)


def _post_batch_after_yield(texts: list[str]) -> list[list[float]]:
    _yield_to_interactive()
    return _post_batch_fit(texts)


def embed_texts(texts: list[str], polite: bool = False) -> list[list[float]]:
    """Embed with dynamic batch sizing + pipelined concurrent requests.

    The GPU-side server (llama-server, --parallel 12) splits one request's
    inputs across slots, but consecutive HTTP requests are serialized —
    GPU idles between them. We therefore cut batches first, then fly
    `request_workers` requests concurrently (order-preserving), keeping all
    slots busy. Prefix caching (LCP) still applies per file since batches
    keep sibling chunks together.

    polite=True（索引构建）：每个 HTTP 批次发送前检查 brain 在途交互请求，
    有则等待让位——索引吞吐永远排在交互流量后面。交互路径（remember/recall/
    search_code/state 缓存）保持默认 False，绝不互相让位。
    """
    cfg = load_config()
    dims = int(cfg.get("truncate_dims", 0))
    max_items = int(cfg.get("batch_size", 32))
    max_tokens = int(cfg.get("max_batch_tokens", 30000))
    item_cap = int(cfg.get("max_item_chars", 12000))
    workers = int(cfg.get("request_workers", 8))

    prepared: list[str] = []
    for t in texts:
        if len(t) > item_cap:  # ~3000 tokens
            head, tail = item_cap * 3 // 4, item_cap // 4
            prepared.append(t[:head] + "\n...\n" + t[-tail:])
        else:
            prepared.append(t)

    # cut batches by item count and token budget
    batches: list[list[str]] = []
    batch: list[str] = []
    batch_tokens = 0
    for t in prepared:
        est = _token_estimate([t])
        if batch and (len(batch) >= max_items or batch_tokens + est > max_tokens):
            batches.append(batch)
            batch, batch_tokens = [], 0
        batch.append(t)
        batch_tokens += est
    if batch:
        batches.append(batch)

    if workers <= 1 or len(batches) <= 1:
        post = _post_batch_after_yield if polite else _post_batch_fit
        raw = [post(b) for b in batches]
    else:
        import concurrent.futures as _cf
        post = _post_batch_after_yield if polite else _post_batch_fit
        with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
            raw = list(ex.map(post, batches))  # ex.map preserves order

    out: list[list[float]] = []
    for group in raw:
        out.extend(_truncate(v, dims) for v in group)
    return out


def embed_query(text: str) -> list[float]:
    cfg = load_config()
    instruction = cfg.get("query_instruction", "")
    dims = int(cfg.get("truncate_dims", 0))
    q = instruction + text if instruction else text
    return _truncate(_post_batch_fit([q])[0], dims)
