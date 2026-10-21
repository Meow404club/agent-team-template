"""阿里云百炼 Batch File 批量嵌入（OpenAI 兼容 /files + /batches 接口）。

用途：索引构建等大批量嵌入走异步批任务——费用仅实时调用 50%；日常检索
（embed_query / state 缓存 / 小增量）恒走 embed.py 直连，互不影响。

流程（与阿里云百炼文档一致，勿凭记忆改）：
  1. 每条文本一行 JSONL：{"custom_id": "<全局序号>", "method": "POST",
     "url": "/v1/embeddings", "body": {"model", "input": <单条文本>,
     "encoding_format": "float"[, "dimensions"]}}
  2. POST {base}/files 上传（multipart，purpose="batch"）→ file id
  3. POST {base}/batches（input_file_id / endpoint="/v1/embeddings" /
     completion_window="24h"）→ batch id
  4. 轮询 GET {base}/batches/{id} 至终态（completed/failed/expired/cancelled）
  5. GET {base}/files/{output_file_id}/content 下载 JSONL，
     按 custom_id→全局序号还原向量顺序

限制：单文件 ≤50000 行且 ≤500MB、单行 ≤6MB；批任务 24h 完成窗口。
鉴权：Authorization: Bearer <api_key>（配置缺失时回退环境变量 DASHSCOPE_API_KEY）。

CLI：python3 tools/brain/batch_embed.py status      # 批任务台账+实时状态
     python3 tools/brain/batch_embed.py cancel <id>
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
JOURNAL_PATH = TOOLS_DIR.parent / "tmp" / "index" / "batch_jobs.jsonl"

# 百炼批任务终态集（validating/in_progress/finalizing/… 均为进行中）
TERMINAL = {"completed", "failed", "expired", "cancelled", "canceled"}
# 外网 API：走系统代理默认行为（embed.py 的无代理 opener 是给本地 GPU 服务的）
_opener = urllib.request.build_opener()


def _api_key(cfg: dict) -> str:
    key = cfg.get("api_key") or os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        raise RuntimeError("batch.api_key 未配置（tools/config.json embedding.batch，"
                           "或环境变量 DASHSCOPE_API_KEY）")
    return key


def _http(url: str, key: str, data: bytes | None = None,
          headers: dict | None = None, timeout: int = 300, retries: int = 3):
    """单次 API 调用（带瞬时错误重试）。返回 (status_code, body_bytes)。"""
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        req.add_header("Authorization", f"Bearer {key}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with _opener.open(req, timeout=timeout) as resp:
                return resp.getcode(), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()  # 4xx/5xx 交给调用方定性，不盲目重试
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(min(30, (2 ** attempt) * 2) + random.random())
    raise RuntimeError(f"batch API 网络失败（重试 {retries} 次）: {url} : {last}")


def _json_call(url: str, key: str, payload: dict | None = None) -> dict:
    code, body = _http(url, key,
                       data=json.dumps(payload).encode() if payload else None,
                       headers={"Content-Type": "application/json"} if payload else None)
    try:
        d = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError(f"batch API 非 JSON 响应 (HTTP {code}): {body[:300]!r}")
    if code >= 400:
        raise RuntimeError(f"batch API HTTP {code}: {json.dumps(d, ensure_ascii=False)[:500]}")
    return d


# ------------------------------------------------------------------ 构建与分片 --

def build_request_line(seq: int, text: str, model: str, dimensions: int) -> str:
    body = {"model": model, "input": text, "encoding_format": "float"}
    if dimensions and dimensions > 0:
        body["dimensions"] = dimensions
    return json.dumps({"custom_id": str(seq), "method": "POST",
                       "url": "/v1/embeddings", "body": body}, ensure_ascii=False)


def split_groups(texts: list[str], cfg: dict) -> list[tuple[int, list[str]]]:
    """按服务限制切组：[(全局序号偏移, 组内文本)]。行数 ≤max_lines_per_file，
    累计字节 ≤max_bytes_per_file（50k 行 × 12k 字符理论上超 500MB，字节闸兜底）。"""
    max_lines = int(cfg.get("max_lines_per_file", 50_000))
    max_bytes = int(cfg.get("max_bytes_per_file", 400_000_000))
    groups: list[tuple[int, list[str]]] = []
    cur: list[str] = []
    cur_bytes = 0
    for i, t in enumerate(texts):
        b = len(t.encode("utf-8")) + 256  # 行 JSON 包装开销估计
        if cur and (len(cur) >= max_lines or cur_bytes + b > max_bytes):
            groups.append((i - len(cur), cur))
            cur, cur_bytes = [], 0
        cur.append(t)
        cur_bytes += b
    if cur:
        groups.append((len(texts) - len(cur), cur))
    return groups


def build_multipart(lines: list[str], boundary: str) -> bytes:
    """files.create 的 multipart/form-data 体：purpose=batch + JSONL 文件。"""
    part = []
    part.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nbatch\r\n".encode())
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    part.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"batch_input.jsonl\"\r\nContent-Type: application/json\r\n\r\n".encode())
    part.append(payload)
    part.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(part)


# ---------------------------------------------------------------- 提交/轮询/回收 --

def _journal(event: dict) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), **event}, ensure_ascii=False) + "\n")


def submit_group(base: str, key: str, offset: int, texts: list[str], cfg: dict) -> dict:
    lines = [build_request_line(offset + i, t, cfg["model"], int(cfg.get("dimensions", 0) or 0))
             for i, t in enumerate(texts)]
    boundary = f"brainbatch{random.randint(1 << 30, 1 << 40):x}"
    body = build_multipart(lines, boundary)
    code, resp = _http(f"{base.rstrip('/')}/files", key, data=body,
                       headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    d = json.loads(resp.decode("utf-8"))
    if code >= 400:
        raise RuntimeError(f"files.create HTTP {code}: {json.dumps(d, ensure_ascii=False)[:500]}")
    file_id = d["id"]

    job = _json_call(f"{base.rstrip('/')}/batches", key, {
        "input_file_id": file_id,
        "endpoint": "/v1/embeddings",
        "completion_window": cfg.get("completion_window", "24h"),
        "metadata": {"description": "brain-batch-embed"},
    })
    _journal({"event": "submitted", "batch_id": job["id"], "file_id": file_id,
              "lines": len(lines), "model": cfg["model"]})
    return {"batch_id": job["id"], "offset": offset, "lines": len(lines)}


def wait_job(base: str, key: str, batch_id: str, cfg: dict) -> dict:
    """轮询至终态。批任务 24h 完成窗口，间隔温和退避；网络抖动容忍连续多次失败。"""
    interval = float(cfg.get("poll_interval_s", 60))
    deadline = time.time() + float(cfg.get("max_wait_s", 93_600))
    net_fails = 0
    while time.time() < deadline:
        try:
            d = _json_call(f"{base.rstrip('/')}/batches/{batch_id}", key)
            net_fails = 0
            status = d.get("status", "?")
            if status in TERMINAL:
                _journal({"event": "terminal", "batch_id": batch_id, "status": status,
                          "request_counts": d.get("request_counts")})
                return d
            print(f"[batch] {batch_id} {status} ...", flush=True)
        except RuntimeError as e:
            net_fails += 1
            if net_fails >= 10:
                raise
            print(f"[batch] {batch_id} 轮询失败（{net_fails}/10）: {e}", flush=True)
        time.sleep(interval)
        interval = min(interval * 1.5, 300)
    raise RuntimeError(f"batch {batch_id} 超过 max_wait_s 未到终态（可 CLI 查询后重试）")


def collect_group(base: str, key: str, job: dict, total: int,
                  vecs: list, missing: list) -> None:
    """下载结果文件，custom_id→全局序号写回 vecs；错误/缺失行记入 missing。"""
    d = _json_call(f"{base.rstrip('/')}/batches/{job['batch_id']}", key)
    out_file = d.get("output_file_id")
    if not out_file:
        raise RuntimeError(f"batch {job['batch_id']} 无 output_file_id: {json.dumps(d, ensure_ascii=False)[:300]}")
    code, body = _http(f"{base.rstrip('/')}/files/{out_file}/content", key, timeout=600)
    if code >= 400:
        raise RuntimeError(f"files.content HTTP {code}: {body[:300]!r}")
    n_ok = 0
    for raw in body.decode("utf-8").splitlines():
        if not raw.strip():
            continue
        line = json.loads(raw)
        seq = int(line.get("custom_id", -1))
        err = line.get("error")
        resp = line.get("response") or {}
        if err or resp.get("status_code") != 200:
            missing.append((seq, str(err or resp)[:200]))
            continue
        try:
            vecs[seq] = resp["body"]["data"][0]["embedding"]
            n_ok += 1
        except (KeyError, IndexError, TypeError) as e:
            missing.append((seq, f"结果行解析失败 {e!r}"))
    if n_ok < job["lines"]:
        absent = job["lines"] - n_ok - sum(1 for s, _ in missing if s >= 0)
        if absent:
            missing.append((-1, f"{absent} 行无结果（终态但输出缺行）"))


def embed_texts_via_batch(texts: list[str], cfg: dict) -> list[list[float]]:
    """同步外观：提交全部组 → 轮询 → 下载 → 按序返回向量。任何组失败即抛错
    （fail-loud：未写库不记账，重跑 --force 重计费）。"""
    base, key = cfg["base_url"], _api_key(cfg)
    groups = split_groups(texts, cfg)
    jobs = [submit_group(base, key, off, g, cfg) for off, g in groups]
    print(f"[batch] 已提交 {len(jobs)} 个批任务共 {len(texts)} 条，等待完成"
          f"（窗口 {cfg.get('completion_window', '24h')}，默认轮询 "
          f"{cfg.get('poll_interval_s', 60)}s）...", flush=True)
    for j in jobs:
        d = wait_job(base, key, j["batch_id"], cfg)
        if d.get("status") != "completed":
            raise RuntimeError(f"batch {j['batch_id']} 终态 {d.get('status')}: "
                               f"{json.dumps(d.get('errors') or {}, ensure_ascii=False)[:300]}")
    vecs: list = [None] * len(texts)
    missing: list = []
    for j in jobs:
        collect_group(base, key, j, len(texts), vecs, missing)
    if missing:
        head = "; ".join(f"#{s}: {m}" for s, m in missing[:3])
        raise RuntimeError(f"batch 嵌入 {len(missing)}/{len(texts)} 行失败/缺失：{head}")
    return vecs


# ------------------------------------------------------------------------- CLI --

def _batch_cfg() -> dict:
    sys.path.insert(0, str(TOOLS_DIR))
    from brain.embed import load_config
    cfg = load_config().get("batch") or {}
    if not (cfg.get("base_url") and (cfg.get("api_key") or os.environ.get("DASHSCOPE_API_KEY"))):
        raise SystemExit("embedding.batch 未配置（base_url/api_key）")
    return cfg


def cli_status() -> None:
    cfg = _batch_cfg()
    if not JOURNAL_PATH.exists():
        print("（无批任务台账）")
        return
    seen: dict[str, dict] = {}
    for line in JOURNAL_PATH.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("batch_id"):
            seen.setdefault(e["batch_id"], e)
    for bid, e in seen.items():
        try:
            d = _json_call(f"{cfg['base_url'].rstrip('/')}/batches/{bid}", _api_key(cfg))
            status = d.get("status", "?")
            counts = d.get("request_counts") or {}
            print(f"{bid:40} {status:12} 提交 {e.get('lines', '?'):>6} 行  "
                  f"完成 {counts.get('completed', '?')} 失败 {counts.get('failed', 0)}")
        except RuntimeError as err:
            print(f"{bid:40} 查询失败: {str(err)[:120]}")


def cli_cancel(batch_id: str) -> None:
    cfg = _batch_cfg()
    d = _json_call(f"{cfg['base_url'].rstrip('/')}/batches/{batch_id}/cancel",
                   _api_key(cfg), payload={})
    _journal({"event": "cancelled", "batch_id": batch_id, "status": d.get("status")})
    print(f"{batch_id}: {d.get('status')}")


def main() -> None:
    signal_pipe()
    ap = argparse.ArgumentParser(description="阿里云百炼批量嵌入批任务管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="批任务台账+实时状态")
    c = sub.add_parser("cancel", help="取消批任务")
    c.add_argument("batch_id")
    args = ap.parse_args()
    if args.cmd == "status":
        cli_status()
    else:
        cli_cancel(args.batch_id)


def signal_pipe():
    import signal
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # head 截断管道属正常用法
    except (AttributeError, ValueError):
        pass


if __name__ == "__main__":
    main()
