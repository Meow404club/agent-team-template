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

## 可选：本地推理服务（ZCode 会话前先拉起）

```bash
LLAMA_BIN=<llama-server 路径> MODEL=<嵌入模型.gguf> tools/embed_server.sh   # → 127.0.0.1:8937
LLAMA_BIN=<llama-server 路径> MODEL=<精排模型.gguf> tools/rerank_server.sh  # → 127.0.0.1:8938
```

不部署本地服务时，把 `tools/config.json` 的 `base_url` 指向任意 OpenAI 兼容
中转/API 即可。精排服务缺席时检索自动降级为纯融合排序（不报错）。

llama.cpp 两个坑：`--ctx-size` 是总 KV 上下文（会被槽平分）；rerank 物理批
`-ub` 必须 ≥ 单条输入 token 数（默认 512 会 500，脚本已设 4096）。

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
| `search_code(query, sources?, limit?, path_glob?)` | 语义+词法混合检索（自动精排） |
| `get_source(file, start?, end?)` | 按相对路径读取原始文件（带行号） |
| `sym_query(pattern, sources?, glob?)` | ripgrep 正则精确搜索 |
| `mappings_lookup(term)` | 映射表双向查询（可选功能） |
| `refresh_index(source?)` | 后台重建/增量更新索引 |
| `remember / recall / forget` | 语义记忆（自动嵌入、去重合并、时效衰减） |
| `kg_add / kg_search / kg_query / kg_del` | 知识图谱：记录/语义检索/精确过滤 |
| `state_read / state_update` | 项目状态长期记忆（任务板/决策/已知 Bug/进度） |
| `project_status()` | 索引规模、KG 条数、worktree 列表、最近提交 |
