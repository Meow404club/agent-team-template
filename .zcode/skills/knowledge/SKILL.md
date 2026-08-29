---
name: knowledge
description: "知识与状态记忆的写入规范：remember/recall 语义记忆、KG 三元组（自动嵌入）、state 账本、任务板。当完成一个研究结论、一个里程碑、发现一个 bug、做出一个架构决策，或用户要求 '记录'、'更新状态'、'记住这个结论' 时使用。"
---

# 知识写入规范

记忆分三层，职责不同（全部通过 MCP `brain`）：

| 层 | 工具 | 记什么 | 特性 |
|---|---|---|---|
| **语义记忆** | `remember` / `recall` / `forget` | 结论性知识：研究结论、决策理由、bug 根因、交接要点 | 自动嵌入；相似≥0.97 去重、≥0.80 合并（mem0 式）；recall 带时效衰减 |
| **知识图谱** | `kg_add` / `kg_search` / `kg_query` | 结构关系：类↔职责、老↔新映射、模块↔依赖 | 三元组自动嵌入，可语义检索（kg_search）也可精确过滤（kg_query） |
| **状态账本** | `state_read` / `state_update` | 时间性事实：进度、TODO、任务板、已知 Bug 列表 | 精确读写，是"唯一真相源"；docs/ 文件只是镜像 |

## 语义记忆（remember）

- `kind` 规范：`decision`（架构决策）| `research`（考古结论）| `bug`（根因+修法）|
  `merge`（合入记录）| `handoff`（跨会话交接）| `lesson`（踩坑教训）。
- text 里**必须带证据**（文件:行号），因为它是会被 `recall` 语义召回的独立知识单元。

## 知识图谱（kg_add）

命名约定：老代码 `LEGACY_`、本项目新代码 `PORT_`、外部平台 API `API_`、
子系统 `SYS_`、参考项目 `REF_`、概念直接用名词（如 `RetryPolicy`）。
（项目可按领域扩充前缀：材质 TEX_、配方 RECIPE_ 等。）

必须记录的关系：`MAPS_TO`（老→新，考古最重要产出）、`DOES`/`OWNS`（职责）、
`DEPENDS_ON`（改动波及面）、`UPGRADES_TO`/`REPLACED_BY`、
`BLOCKED_BY`（任务依赖）、`TRAPS`（陷阱→正确做法）、`LANDED`（已合入 main）。
`node_types`：Class|System|API|Concept|Task|Material。

## 状态账本（state_update）

- `tasks`：任务板（唯一真相源），`{"<slug>": {"status","branch","worktree","commits","owner","files_scope","note"}}`，
  status ∈ research|queued|in_progress|in_review|merged|aborted。
- `decisions`：merge 追加 `{"topic","decision","alternatives","evidence","date"}`。
- `known_bugs`：merge 追加 `{"symptom","root_cause","fix","status"}`。
- `progress`：覆盖式 `{"phase","done":[],"current","next"}`。
- 重要变更后同步镜像到 `docs/PROJECT_STATE.md` / `docs/TODO.md`。

## 读规则

任何 Agent 动手前：`recall()` + `state_read()` + `kg_search()` 先行——
已有结论不许重新考古一遍。
