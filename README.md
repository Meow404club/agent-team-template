# Agent Team Template · ZCode 多 Agent 协作框架模板

把一个工程目标交给一支 AI 团队：主会话当**组织者**，五个专业角色 subagent 像开源
项目一样并行干活——独立 git worktree、GPG 签名原子提交、专职审查合并官把守 main。
配套本地 **RAG 检索 + 三层记忆中枢**（brain MCP），让每个 Agent 动手前先查已有结论，
干完后把知识落账，跨会话不丢。

```
              ┌────────────────────────────────────────────┐
              │ 主会话 = 组织者 (AGENTS.md，只加载于主入口)      │
              │ 拆解目标 → 任务卡 → 并行派发 → 收 hash → 落账   │
              └──────┬───────────┬───────────┬─────────────┘
        ┌────────────┼───────────┼───────────┼──────────────┐
   architect    researcher   coder ×2~4   review-merge   debugger
   模块卡/ADR    调研/查证/选型  worktree实现  审查+合并main   根因+修复
        └──────┬─────┴───────────┴─────┬─────┴──────────────┘
               │ harvest 落盘资料        │
          curator（资料策展人）：噪声剔除 → 裁决入库 → 增量索引 → 检索验证
                                       │
                    brain MCP（tools/brain/，SQLite）
        混合检索(向量+BM25+精排) · 语义记忆 · 知识图谱 · 状态账本
                                       │
        约束层：PreToolUse 拦裸 commit · commit-msg 格式 · pre-push 验签
```

框架 = **记忆 / 协作 / 约束** 三位一体：

| 层 | 载体 | 说明 |
|---|---|---|
| 记忆 | `tools/brain/`（MCP `brain`） | 语义记忆（自动嵌入、mem0 式去重合并）、KG 三元组、状态账本（任务板=唯一真相源） |
| 协作 | `AGENTS.md` + `.zcode/agents|commands|skills` | 组织者宪法 + 六角色提示词 + 派发命令；worktree `../<仓库名>-trees/<slug>` + `work/<slug>` 分支 |
| 约束 | `.githooks/` | GPG 双层强制（AI 发起时拦截 / git 层校验 / push 验签）+ 密钥与大文件拦截 |

检索内核参考了业界实践：cAST 结构感知分块（arXiv:2506.15655）、Anthropic
Contextual Retrieval（上下文前缀 + BM25 混合 + 加权 RRF）、Cross-Encoder 精排、
mem0 式语义记忆、GraphRAG 的轻量等价物（KG 即图）。

## 快速开始

前置：Python 3.10+（或 uv）、git、ripgrep（`rg`，sym_query 依赖）；
可选：GPG 私钥（提交签名）、llama.cpp（本地 GPU 推理）。

```bash
# 1) 从模板建你的项目（重建 git 历史，不带模板提交记录）
git clone git@github.com:Meow404club/agent-team-template.git my-project
cd my-project && rm -rf .git && git init -b main

# 2) 一键初始化（venv + 依赖 + 本地配置 + git 钩子）
./scripts/setup.sh

# 3) 定制（详见下方"必改清单"），然后建索引
tools/.venv/bin/python tools/brain/index.py all

# 4) 起常驻服务：brain MCP 必启（HTTP :8939/mcp）；embed/rerank 可选（需 LLAMA_BIN）
tools/services.sh start          # 或 tools/services.sh start brain

# 5) 用 ZCode 打开 my-project —— 确认 MCP 面板里 brain 已连接
#    第一句话就说：恢复上下文，开始 <你的目标>
```

嵌入后端二选一（`tools/config.json`）：
- **本地 GPU**（推荐）：跑 `tools/embed_server.sh` + `tools/rerank_server.sh`，
  默认指向 `http://127.0.0.1:8937/v1`；
- **远端 API**：把 `base_url/model/api_key` 换成任意 OpenAI 兼容中转。

## 必改清单（复制模板后）

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `AGENTS.md` | 项目名、目录地图（`tmp/` 下放什么资料）、领域相关铁律 |
| 2 | `tools/sources.json` | 资料源注册表：上游/老源码、平台 API、官方文档、参考实现（由 `sources.example.json` 生成） |
| 3 | `tools/config.json` | 嵌入 API 与 `query_instruction`（领域描述） |
| 4 | `.zcode/agents/*.md` | 把 `<legacy>`/`<platform-api>` 等占位换成你的 sources key；补充领域红线（review-merge 的架构红线、debugger 的病灶速查） |
| 5 | `docs/` 骨架 | PROJECT_STATE.md / TODO.md / RESEARCH-NOTES.md 填入项目信息 |

可选：`.githooks/guard-commit.sh` 无需改动（自动探测仓库与 worktree 约定目录）；
注册到 ZCode 见 `.githooks/README.md`（推荐用户级，免信任审核弹卡）。

## 日常使用（主会话）

- `/architect <目标>` 拆解系统出模块卡；`/researcher <问题>` 调研查证
  （代码考古 / 联网调研 / 方案选型）
- `/coder <任务卡>` 并行派发实现（2~4 个，文件域不重叠）；返回 hash 后立即
  `/review-merge work/<slug>` 串行合并
- `/debugger <症状>` 排障；每次合并/决策/bug 后自动落账 state + KG + 语义记忆
- 会话开头说"恢复上下文"触发 context-loader skill

## FAQ

- **MCP brain 未连接**：brain 是常驻 HTTP 服务（127.0.0.1:8939/mcp），不是 ZCode
  自动拉起的 stdio 进程——先跑 `tools/services.sh start brain`（或 status 看状态），
  再重启/重连 ZCode 会话。刻意不用 FastMCP/stdio：anyio 线程层在长驻进程里会卡死
  工具调用，HTTP 守护还能让主会话与 subagent 共享同一实例、随时探活重启。
- **git commit 被拦**：这是钩子在强制 GPG——用 `git commit -S -s`，消息含
  `<type>(<scope>): <主题>` + `Task:` 行。
- **search_code 报嵌入服务不可达**：没起本地服务时改用远端 API 配置，或
  `tools/services.sh start embed`。
- **include 匹配不到文件**：`**` 是 fnmatch 语义（非递归），根目录文件要单独写一条。

## 目录结构

```
AGENTS.md            组织者宪法（主 Agent 专用，subagent 不加载）
.zcode/config.json   MCP 服务器 brain 注册（type:http → 127.0.0.1:8939/mcp）
.zcode/agents/       六角色 subagent 模板（系统提示词）
.zcode/commands/     /architect /researcher /coder /review-merge /debugger /curator
.zcode/skills/       context-loader · knowledge（记忆写入规范）
.githooks/           GPG 双层强制钩子 + 注册说明
tools/brain/         RAG 检索 + 记忆后端（常驻 HTTP MCP 守护，纯标准库协议壳）
tools/services.sh    服务总线：brain / embed / rerank 三服务统一启停
scripts/setup.sh     一键初始化
docs/                状态镜像（PROJECT_STATE/TODO/RESEARCH-NOTES）
tmp/                 外部资料（老源码/参考实现/映射表）+ 索引（gitignore）
```
