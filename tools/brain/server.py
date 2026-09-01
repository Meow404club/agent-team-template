#!/usr/bin/env python3
"""Brain — project MCP server (Streamable HTTP), hand-rolled JSON-RPC.

Deliberately NOT FastMCP: the anyio thread layer repeatedly deadlocked on
tool calls in long-lived stdio processes (same path runs 0.06s standalone).
Streamable HTTP transport instead:
- resident daemon, independent of ZCode session lifecycle (no per-session
  spawn, no orphan processes)
- main session + subagents share one instance
- probe with GET /health anytime; restart independently

Protocol: POST /mcp accepts JSON-RPC (single or batch), processed
synchronously, returns application/json (no server-initiated push, so GET
on /mcp returns 405 per spec). All business logic lives in
brain.search / brain.memory / brain.web — this file is only the protocol shell.

Start: tools/services.sh start brain (logs: tmp/index/brain-server.log)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import brain.search as S  # noqa: E402
import brain.memory as M  # noqa: E402
import brain.web as W  # noqa: E402
import brain.harvest as HV  # noqa: E402
import brain.autorefresh as AR  # noqa: E402
from brain import db  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("BRAIN_PORT", "8939"))
SERVER_INFO = {"name": "brain", "version": "2.3-http"}
# 随 initialize 下发、注入所有 agent 的全局引导
INSTRUCTIONS = (
    "本项目的记忆与检索中枢。动手前先检索，按需选工具："
    "不确定确切类名/方法名、按概念或行为意图找代码 → search_code（语义+词法混合检索+精排）；"
    "已知确切符号名 → sym_query（ripgrep 快速精确定位）；"
    "命中后 get_source 通读原文——不要凭记忆猜 API。重要结论写入 remember/state 与 KG。"
)

SESSIONS: set[str] = set()


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


# ------------------------------------------------------------ 工具实现 --

def tool_refresh_index(source: str | None = None) -> str:
    log = open(db.PROJECT_ROOT / "tmp" / "index" / "refresh.log", "a", encoding="utf-8")
    targets = [source] if source else list(db.load_sources().keys())
    subprocess.Popen(
        [sys.executable, str(TOOLS_DIR / "brain" / "index.py"), *targets],
        cwd=str(TOOLS_DIR), stdout=log, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    return _j({"ok": True, "targets": targets, "log": "tmp/index/refresh.log"})


def _guarded(fn, **kwargs) -> str:
    try:
        return _j(fn(**kwargs))
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        return _j({"error": str(e)})


# 工具表：impl=实现；params=[(名, 类型, 必填, 描述)]；desc=工具描述（agent 提示词引用过）
T = lambda name, type_, req, desc: {"name": name, "type": type_, "required": req, "desc": desc}

TOOLS: dict[str, dict] = {
    "search_code": dict(
        impl=S.search_code,
        params=[T("query", "string", True, "语义检索查询词（自然语言概念/意图描述，不是符号名）"),
                T("sources", "array", False, "资料源子集，key 见 tools/sources.json"),
                T("limit", "integer", False, "返回条数"),
                T("path_glob", "string", False, "文件名 glob 过滤")],
        desc="语义+词法混合检索已索引的源码/文档（向量+BM25+RRF+Cross-Encoder 精排；资料源清单见 "
             "tools/sources.json，典型含上游老源码、平台 API、官方文档、现代参考实现、项目 docs）。\n\n"
             "适用：不知道确切类名/方法名、按概念或行为意图找代码、跨资料源找同类实现。\n"
             "已知确切符号名时 sym_query 更快；本工具命中后用 get_source 通读原文。\n"
             "sources 可选值 = sources.json 的 key（用 project_status() 可查看）。省略则检索全部。\n"
             "path_glob 如 \"*Service*.java\"。返回相关代码块（含文件路径、行号、片段）。"),
    "get_source": dict(
        impl=S.get_source,
        params=[T("file", "string", True, "仓库根相对路径（search_code 返回的 path 原样传入）"),
                T("start", "integer", False, "起始行"),
                T("end", "integer", False, "结束行")],
        desc="按仓库根相对路径精确阅读原始文件（带行号）。file 是 search_code 返回的 path 字段原样传入。"),
    "sym_query": dict(
        impl=S.sym_query,
        params=[T("pattern", "string", True, "ripgrep 正则"),
                T("sources", "array", False, "资料源子集"),
                T("glob", "string", False, "文件名 glob"),
                T("limit", "integer", False, "返回条数")],
        desc="ripgrep 正则精确搜索：已知确切类名/方法名/字符串常量时的快速定位。\n"
             "例: pattern=\"class UserService\" glob=\"*.java\"。\n"
             "不确定符号名、或按概念/行为找代码时，先用 search_code 语义检索。"),
    "mappings_lookup": dict(
        impl=S.mappings_lookup,
        params=[T("term", "string", True, "混淆/代号名或官方名")],
        desc="查询映射表：混淆/代号名 ↔ 官方名（类/字段/方法双向模糊匹配）。可选功能——把映射文件放到 tmp/mappings/（或设 BRAIN_MAPPINGS_FILE 环境变量）后可用；无映射文件时返回空结果。"),
    "web_fetch": dict(
        impl=W.web_fetch,
        params=[T("url", "string", True, "http(s) URL"),
                T("timeout", "integer", False, "超时秒数（默认 20）"),
                T("max_chars", "integer", False, "正文截断长度（默认 30000）"),
                T("raw", "boolean", False, "true=返回原始 HTML，默认转纯文本")],
        desc="抓取网页（curl_cffi 浏览器 TLS 指纹，可过 TLS 层反爬；需执行 JS 的挑战页过不了）。\n"
             "默认 HTML 转纯文本；返回 url(重定向后)/status/content_type/truncated；403/503 等反爬页原样返回内容以便判断封锁原因。\n"
             "与 WebSearch 配合：先搜索，再用本工具精读目标页正文。"),
    "harvest": dict(
        impl=HV.harvest,
        params=[T("url", "string", True, "http(s) URL；repo 支持 GitHub codeload tarball 或任意 .tar.gz/.tgz"),
                T("name", "string", True, "kebab-case 资料名（落盘目录 tmp/harvest/<name>/）"),
                T("kind", "string", False, "auto|page(HTML→Markdown)|file(原样)|repo(tar.gz 安全解包剥顶层)；默认 auto"),
                T("raw", "boolean", False, "page 类容许原始落盘的开关（true 时 HTML 不转 Markdown）"),
                T("timeout", "integer", False, "超时秒数（默认 30）")],
        desc="资料收割：把要反复参考的外部资料落盘到 tmp/harvest/<name>/（researcher 专用）。"
             "page=网页转 Markdown；file=原样下载；repo=GitHub tarball/.tar.gz 解包（防路径穿越、"
             "限额保护，自动剥离顶层 distdir）。只落盘、绝不触碰索引——落盘即可用 "
             "get_source(file='tmp/harvest/...') 与 sym_query(sources=['harvest']) 阅读；"
             "是否入 RAG 由 curator 角色审查后决策（refresh_index(source='harvest') 增量索引，"
             "裁剪规则 tmp/harvest/exclude.json 即时生效）。"),
    "refresh_index": dict(
        impl=tool_refresh_index,
        params=[T("source", "string", False, "只刷新指定资料源；空则全部")],
        desc="后台增量重建索引（新克隆/更新的资料需要 refresh 后才能被 search_code 检索）。source 为空则刷新全部。立即返回，进度见 tmp/index/refresh.log。"),
    "remember": dict(
        impl=M.remember,
        params=[T("kind", "string", True, "记忆类型: decision|research|bug|merge|handoff|lesson"),
                T("text", "string", True, "记忆正文")],
        desc="写入语义记忆（自动嵌入）。kind 建议: decision|research|bug|merge|handoff|lesson。\n"
             "相似度≥0.97 视为重复忽略；≥0.80 追加合并进旧记忆（mem0 式 UPDATE）。"),
    "recall": dict(
        impl=M.recall,
        params=[T("query", "string", True, "语义查询"),
                T("k", "integer", False, "返回条数"),
                T("kind", "string", False, "限定记忆类型")],
        desc="语义回忆历史记忆（带时效衰减，活跃优先）。会话开始、动手前先 recall，避免重复调研。"),
    "forget": dict(
        impl=M.forget,
        params=[T("memory_id", "integer", True, "记忆 id")],
        desc="软删除一条语义记忆（保留审计痕迹）。"),
    "kg_add": dict(
        impl=M.kg_add_embedded,
        params=[T("src", "string", True, "源实体"),
                T("rel", "string", True, "关系"),
                T("dst", "string", True, "目标实体"),
                T("note", "string", False, "备注"),
                T("node_types", "object", False, "节点类型映射，如 {\"节点名\": \"Class\"}")],
        desc="知识图谱记录一条关系：src -[rel]-> dst（例: kg_add(\"LEGACY_OrderService\",\"MAPS_TO\",\"PORT_OrderManager\")）。\n"
             "三元组会同时嵌入语义索引（可用 kg_search 语义检索）。\n"
             "node_types 可选 {\"节点名\": \"Class|System|API|Concept|Task|Material\"}。"),
    "kg_search": dict(
        impl=M.kg_search,
        params=[T("query", "string", True, "语义查询"),
                T("k", "integer", False, "返回条数")],
        desc="语义检索知识图谱三元组（如 \"重试逻辑怎么做的\"）。精确过滤用 kg_query。"),
    "kg_query": dict(
        impl=S.kg_query,
        params=[T("entity", "string", False, "实体名模糊过滤"),
                T("rel", "string", False, "关系类型过滤"),
                T("limit", "integer", False, "返回条数")],
        desc="检索知识图谱：按实体名或关系类型模糊查询。"),
    "kg_del": dict(
        impl=S.kg_del,
        params=[T("entity", "string", True, "实体名")],
        desc="从知识图谱物理删除一个实体及其所有关系。⚠️ 关系过时请用 kg_invalidate（保留演化链），本工具只用于清理错误/烟雾数据。"),
    "kg_invalidate": dict(
        impl=S.kg_invalidate,
        params=[T("src", "string", True, "源实体"),
                T("rel", "string", True, "关系"),
                T("dst", "string", True, "目标实体"),
                T("reason", "string", False, "失效原因（记入审计）")],
        desc="关系过时的正规入口（双时态）：置 invalid_at 保留历史，不物理删除。"
             "新事实取代旧关系时：先 kg_invalidate 旧边，再 kg_add 新边。"),
    "state_read": dict(
        impl=S.state_read,
        params=[T("key", "string", False, "限定 key；空则返回全部（每 key 最近 10 条，最新在前）"),
                T("limit", "integer", False, "无 key 概览/列表倒序时的截断条数（默认 10）；读全量请指定 key 并加大 limit")],
        desc="读取项目状态记忆。key 可选: decisions, todo, known_bugs, progress, architecture, tasks, harvest_log；为空返回全部"
             "——但按最近更新排序、列表/记录型只取最近 10 条（最新在前），读全量请指定 key（必要时 limit 调大）。"),
    "state_update": dict(
        impl=S.state_update,
        params=[T("key", "string", True, "状态键（点分 namespace，如 tasks.xxx / tmp.xxx）"),
                T("value", "any", True, "JSON 兼容结构"),
                T("merge", "boolean", False, "true=深合并（dict 递归更新子典/list 追加去重），false=整体覆盖"),
                T("ttl_seconds", "number", False, "可选：N 秒后过期（惰性清扫）。临时键用 tmp.* 前缀+TTL")],
        desc="写入项目状态记忆。key 建议: decisions(决策+理由), todo, known_bugs, progress, architecture, tasks。\n"
             "merge=true 深合并：dict+dict 递归更新子典（可只传变更字段，不清空任务卡等既有结构）、"
             "list+list 追加去重、其余/类型不匹配新值覆盖；整体替换/缩短列表用 merge=false。\n"
             "value 必须是 JSON 兼容结构。"),
    "state_search": dict(
        impl=S.state_search,
        params=[T("query", "string", True, "语义查询（自然语言）"),
                T("k", "integer", False, "返回条数"),
                T("prefix", "string", False, "namespace 前缀过滤（如 'tasks.' / 'tmp.'）"),
                T("offset", "integer", False, "分页偏移")],
        desc="语义检索 state 账本（值嵌入缓存，写入时更新）：top-k 返回 key+预览+分数，命中后 state_read(key) 读全量。"
             "prefix 按 namespace 过滤；与 LangGraph BaseStore search 同构。"),
    "project_status": dict(
        impl=S.project_status,
        params=[],
        desc="项目总览：各资料源索引规模、KG 条数、状态键、git worktree 列表、最近提交。"),
}


def _coerce(params_spec, args: dict) -> dict:
    """按工具表做宽松类型规整（pydantic 的替代品）。关键字传参：缺省可选参直接省略，
    交给 impl 的默认值——绝不用位置传参，否则跳过可选参会导致后参左移错位。"""
    declared = {p["name"]: p for p in params_spec}
    unknown = set(args) - set(declared)
    if unknown:
        raise ValueError(f"未知参数: {sorted(unknown)}")
    kwargs = {}
    for p in params_spec:
        if p["name"] not in args:
            if p["required"]:
                raise ValueError(f"缺少必填参数: {p['name']}")
            continue
        v = args[p["name"]]
        if p["type"] == "integer":
            v = int(v)
        elif p["type"] == "number":
            v = float(v)
        elif p["type"] == "boolean":
            v = bool(v)
        elif p["type"] == "string":
            v = str(v)
        elif p["type"] == "array":
            if v is not None and not isinstance(v, list):
                v = [v]
        kwargs[p["name"]] = v
    return kwargs


def _input_schema(params_spec) -> dict:
    ty = {"string": "string", "integer": "integer", "number": "number",
          "boolean": "boolean", "array": "array", "object": "object", "any": None}
    props = {}
    for p in params_spec:
        t = ty[p["type"]]
        prop = {"description": p["desc"]}
        if isinstance(t, str):
            prop["type"] = t
        if t == "array":
            prop["items"] = {"type": "string"}
        props[p["name"]] = prop
    return {"type": "object", "properties": props,
            "required": [p["name"] for p in params_spec if p["required"]]}


# ------------------------------------------------------------------ 协议层 --

def _reply(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _reply_err(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_message(msg: dict) -> dict | None:
    """处理单条 JSON-RPC 消息。notification 返回 None，请求返回响应对象。
    同步处理：一个 HTTP 请求线程跑完一个调用（嵌入预算已收口在 ~45s 内）。"""
    method = msg.get("method")
    req_id = msg.get("id")  # notification 时为 None

    if method == "initialize":
        client_v = (msg.get("params") or {}).get("protocolVersion", "2024-11-05")
        res = _reply(req_id, {
            "protocolVersion": client_v,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })
        res["_session"] = uuid.uuid4().hex  # 由 HTTP 层转成 Mcp-Session-Id 响应头
        SESSIONS.add(res["_session"])
        return res
    elif method == "notifications/initialized":
        return None
    elif method == "notifications/cancelled":
        return None
    elif method == "ping":
        return _reply(req_id, {})
    elif method == "tools/list":
        tools_out = []
        for name, spec in TOOLS.items():
            entry = {"name": name, "description": spec["desc"]}
            try:
                entry["inputSchema"] = _input_schema(spec["params"])
            except Exception as e:  # 单工具 schema 缺陷降级，绝不让 tools/list 整体失败
                entry["inputSchema"] = {"type": "object"}
                print(f"tools/list: schema fallback for {name}: {e!r}", file=sys.stderr, flush=True)
            tools_out.append(entry)
        return _reply(req_id, {"tools": tools_out})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        spec = TOOLS.get(name)
        if spec is None:
            return _reply_err(req_id, -32602, f"unknown tool: {name}")
        try:
            kwargs = _coerce(spec["params"], args)
        except Exception as e:
            return _reply_err(req_id, -32602, str(e))
        text = _guarded(spec["impl"], **kwargs)
        # 只带 structuredContent（content 留空保持规范兼容）——同一份 JSON 双份注入
        # 纯浪费上下文。工具返回值本就是 JSON 字符串，解成原生对象放进
        # structuredContent，省掉一层引号/换行转义（模型直接读结构化数据）。
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            payload = {"result": text}
        return _reply(req_id, {"content": [],
                               "structuredContent": payload, "isError": False})
    else:
        if req_id is not None:
            return _reply_err(req_id, -32601, f"method not found: {method}")
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默默认访问日志，错误走 stderr
        pass

    def _send_json(self, obj, status: int = 200, extra_headers: dict | None = None):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"status": "ok", "tools": len(TOOLS), "sessions": len(SESSIONS)})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/mcp":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(_reply_err(None, -32700, f"parse error: {e}"), 400)
            return

        extra = {}
        responses = []
        messages = payload if isinstance(payload, list) else [payload]
        for m in messages:
            r = handle_message(m if isinstance(m, dict) else {})
            if r is None:
                continue
            sid = r.pop("_session", None)
            if sid:
                extra["Mcp-Session-Id"] = sid
            responses.append(r)
        if not responses:
            self._send_json({}, 202)  # 纯 notification：202 Accepted
            return
        out = responses if isinstance(payload, list) else responses[0]
        self._send_json(out, 200, extra)

    def do_DELETE(self):
        sid = self.headers.get("Mcp-Session-Id")
        if sid:
            SESSIONS.discard(sid)
        self._send_json({"ok": True})


def main() -> None:
    if AR.start() is not None:
        print("autorefresh: periodic incremental indexing thread started", file=sys.stderr, flush=True)
    print(f"brain http server listening on http://{HOST}:{PORT}/mcp "
          f"({len(TOOLS)} tools)", file=sys.stderr, flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
