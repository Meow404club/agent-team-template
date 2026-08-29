---
description: 派发架构师子代理（architect）：拆解目标系统、制定里程碑与架构红线 ADR。产出模块卡，不写功能代码。
---
用 Agent 工具派发 `subagent_type: architect`（角色系统提示词在 .zcode/agents/architect.md，无需复述）。

任务输入：$ARGUMENTS

派发 prompt 必须自包含（子代理不加载本项目 AGENTS.md），至少包含：
1. 目标：要拆解/决策什么（附项目背景与阶段目标）
2. 上下文：先用 brain 的 state_read/recall 查好相关结论，摘录贴入
3. 期望产出：模块卡（依赖/风险/验收/files_scope）+ ADR 落账 + 任务板建议
4. 边界：只做拆解与决策，不写功能代码

子代理返回后（主 Agent 落账）：state_update(tasks/decisions) + kg_add + docs 镜像同步。
