# Brain 工具链（本地检索与记忆基础设施）

为多 Agent 协作框架提供的 RAG + 记忆后端。全部工具通过项目级 MCP 服务器 `brain` 暴露给 ZCode。

## 组成

- `brain/` —— Python 包：
  - `index.py` 索引器：扫描 `sources.json` 资料源，cAST 结构感知分块（Java 用
    tree-sitter；Markdown 按节且带标题层级），调用嵌入模型向量化，存入 SQLite
    （`tmp/index/rag.db`）。支持增量（按 mtime/size 跳过未变化文件）。
  - `server.py` MCP 服务器（stdio）：语义检索、精确符号搜索、原文阅读、映射表查询、
    知识图谱（KG）、项目状态记忆。
  - `search.py` 混合检索：语义（向量点积）+ 词法（FTS5 bm25）加权 RRF 融合，
    可选 Cross-Encoder 精排（本地 `/v1/rerank`，失败自动降级）。
  - `embed.py` 嵌入客户端：任意 OpenAI 兼容端点；动态 token 预算分批 + 并发请求。
  - `db.py` / `memory.py` / `chunking.py`：存储、语义记忆（mem0 式去重合并）、分块。
- `config.json` —— 嵌入 API 配置（**含密钥，已 gitignore；模板见 `config.example.json`**）。
- `sources.json` —— 资料源注册表（**随项目定制；模板见 `sources.example.json`**）。
  字段：`root`（相对仓库根）、`lang`（java/md）、`include`/`exclude`、`desc`。
  ⚠ include/exclude 按 fnmatch 匹配相对路径，`docs/**/*.md` 不匹配 `docs/a.md`
  （`**` 非递归语义），根目录文件需另写 `docs/*.md`。
- `embed_server.sh` / `rerank_server.sh` —— 可选的本地 GPU 推理（llama.cpp）。

## 手动用法

```bash
VENV=tools/.venv/bin/python
$VENV tools/brain/index.py all                 # 全量/增量索引所有源（任意 cwd 可跑）
$VENV tools/brain/index.py legacy platform-api # 只索引指定源（名字=sources.json 的 key）
$VENV tools/brain/index.py --limit-files 5 all # 小规模试跑
# 索引规模总览用 MCP 工具 project_status()，或看 tmp/index/ 下的日志
```

## MCP 服务器：常驻 HTTP 守护（:8939/mcp）

brain 不是 ZCode 自动拉起的 stdio 进程，而是独立守护进程（主会话与 subagent 共享
同一实例）。刻意不用 FastMCP：anyio 线程层在长驻 stdio 进程中出现过工具调用卡死。
现实现为 ThreadingHTTPServer + POST /mcp 同步 JSON-RPC（单条/batch）+ GET /health
探活 + Mcp-Session-Id 会话，仅用 Python 标准库。`.zcode/config.json` 以 `type:http`
直连端点。

```bash
tools/services.sh start brain     # 起服务（日志 tmp/index/brain-server.log）
curl -s 127.0.0.1:8939/health     # 探活 {"status":"ok","tools":16,...}
tools/services.sh status          # 三服务总览
```

烟测示例：

```bash
curl -s 127.0.0.1:8939/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 300
```

## 服务总线

```bash
tools/services.sh {start|stop|restart|status} [brain|embed|rerank|all]
```

- `brain`：上文的 MCP 守护（无需 GPU/额外依赖）
- `embed`：复用 `embed_server.sh`（需 `LLAMA_BIN` 指向 llama-server，模型放
  `tmp/models/` 或用 `MODEL` 环境变量）
- `rerank`：内联参数起 llama-server `--rerank`（需 `LLAMA_BIN`；模型
  `RERANK_MODEL` 环境变量，默认 `tmp/models/Qwen3-Reranker-0.6B.Q8_0.gguf`）
- 探活只认 HTTP 200（llama 模型加载期 /health 是 503）；按监听端口精准启停，
  不误杀无关进程

## 索引分块语言

cAST 结构感知分块：Java（专用精细路径）+ 通用 AST 分块覆盖 python / js / ts /
tsx / go / rust / c / cpp（tree-sitter 官方 grammar，缺包自动跳过）+ Markdown
按节分块 + 其余扩展名 hard_split。资料源 `lang: "auto"` 时按扩展名逐文件解析
（harvest 源默认 auto）。

## 资料收割管线（harvest → curator → 增量索引）

1. researcher 调研中用 `harvest(url, name, kind)` 把反复参考的资料落盘
   `tmp/harvest/<name>/`：`page`（HTML→Markdown，html2text）、`file`（原样）、
   `repo`（GitHub codeload tarball / 任意 .tar.gz 安全解包、剥顶层 distdir、
   防路径穿越、限额保护）。写 meta.json 供审查。**只落盘绝不触碰索引**——
   不阻塞 MCP 调用；落盘即可 `get_source`/`sym_query(sources=['harvest'])` 阅读。
2. 主 agent 把 harvest 清单派给 **curator** 角色（资料策展人）：抽样审查 →
   剔除会污染检索的噪声子目录（vendor/tests/构建产物…，规则写
   `tmp/harvest/exclude.json`，glob 数组、`load_sources` 动态合并、不入库、
   即时生效）→ `refresh_index(source="harvest")` 增量索引 → 轮询
   `tmp/index/refresh.log` → `search_code(sources=["harvest"])` 验证 →
   `state_update(key="harvest_log")` 落账。
3. curator 只动 tmp/harvest 与 exclude 规则，无 git 操作、不占主 agent 上下文。

## 可选：映射表查询

把映射文件放到 `tmp/mappings/mappings.txt`（或设 `BRAIN_MAPPINGS_FILE`），
`mappings_lookup` 即可双向查询混淆名↔官方名。无映射文件时该工具返回空结果。

## 环境变量

| 变量 | 作用 |
|---|---|
| `BRAIN_RAG_DB` / `BRAIN_RAG_CONFIG`…（`BRAIN_` 前缀） | 覆盖默认的 db/config 路径 |
| `BRAIN_RERANK_URL` | 精排服务地址（默认 `http://127.0.0.1:8938/v1`） |
| `BRAIN_MAPPINGS_FILE` | 映射表文件路径（默认 `tmp/mappings/mappings.txt`） |

## MCP 工具一览（服务器名 brain）

| 工具 | 用途 |
|---|---|
| `search_code(query, sources?, limit?, path_glob?)` | 语义+词法混合检索（自动精排）。**不确定确切类名/方法名、按概念或行为意图查代码时用它**；已知确切符号名用 sym_query 更快 |
| `get_source(file, start?, end?)` | 按相对路径读取原始文件（带行号） |
| `sym_query(pattern, sources?, glob?)` | ripgrep 正则精确搜索：**已知确切类名/方法名/字符串时的快速定位** |
| `web_fetch(url, timeout?, max_chars?, raw?)` | 抓网页（curl_cffi 浏览器 TLS 指纹）：HTML 自动转纯文本，raw=true 返回原始 HTML。能过 TLS 指纹层反爬（实测 zillow 等 urllib 403 页）；需执行 JS 的挑战页（如 g2.com）过不了，需真浏览器方案 |
| `harvest(url, name, kind?, raw?, timeout?)` | 资料收割到 tmp/harvest/<name>/（page 网页转 Markdown / file 原样 / repo tar.gz 安全解包，支持 GitHub codeload）。只落盘不索引，落盘即可 get_source/sym_query 阅读；入 RAG 由 curator 角色裁决 |
| `mappings_lookup(term)` | 映射表双向查询（可选功能） |
| `refresh_index(source?)` | 后台重建/增量更新索引 |
| `remember / recall / forget` | 语义记忆（自动嵌入、去重合并、时效衰减） |
| `kg_add / kg_search / kg_query / kg_del` | 知识图谱：记录/语义检索/精确过滤 |
| `state_read / state_update` | 项目状态长期记忆（任务板/决策/已知 Bug/进度） |
| `project_status()` | 索引规模、KG 条数、worktree 列表、最近提交 |
