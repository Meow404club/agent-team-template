---
name: context-loader
description: "项目会话上下文装载器。每次会话开始、或任何 Agent 接到任务准备动手前使用：恢复项目状态记忆（任务板/决策/已知Bug/进度），汇报检索库覆盖情况，并给出下一步行动指引。当用户提到 '恢复上下文'、'项目状态'、'继续' 或任何会话开始需要项目背景时也应触发。"
---

# 上下文装载

在开始任何工作之前，按顺序执行：

1. 用 MCP 工具 `state_read` 读取全部项目状态（tasks 任务板 / decisions / known_bugs / progress / architecture）。
2. 用 `recall("<本次目标关键词>")` 语义回忆历史结论（研究/决策/交接），避免重复考古。
3. 用 `project_status()` 查看检索索引规模、KG 条数、worktree 列表与最近提交。
4. 读 `docs/PROJECT_STATE.md` 与 `docs/TODO.md`（镜像；MCP state 为权威）。
5. 若任务板有 in_progress/in_review 任务，先核对对应 worktree 与分支状态再行动。

## 汇报格式

```
◆ <项目名> · 上下文恢复
· 阶段: <progress.phase>
· 任务板: <tasks 各 slug 状态一行一个>
· 最近决策: <decisions 最新 3 条主题>
· 已知 Bug: <known_bugs 数量> 项
· 活跃 worktree: <列表>
· 建议: <下一步>
```

## 冷启动（首次 / 索引为空）

若 `project_status()` 显示索引为空：
1. 提示用户索引尚未建立。
2. 后台执行 `tools/.venv/bin/python tools/brain/index.py all`（日志 `tmp/index/full-index.log`）。
3. 完成前不要进行需要检索的工作；完成后 `state_update(key="progress", ...)` 记录。

## 铁律提醒（装载后必须遵守）

- 不猜测任何外部 API：动手前用 `search_code` / `sym_query` / `get_source` 查证。
- 提交必须 GPG 签名（`git commit -S -s`）且经 review-merge Agent 审查（流程见 AGENTS.md 并行 PR 工作流）。
- 每完成一个子任务：`remember`/`state_update` 落账 + `kg_add` 记录关键关系。
