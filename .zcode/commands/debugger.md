---
description: 派发 QA 除虫子代理（debugger）：跑构建、读崩溃报告、修依赖/运行时/时序类 Bug，根因记入记忆。
---
用 Agent 工具派发 `subagent_type: debugger`。

任务输入：$ARGUMENTS（崩溃日志路径 / Bug 描述 / 构建输出；留空则先跑构建摸底）

派发 prompt 必须自包含，至少包含：
1. 仓库根绝对路径 + 症状与复现方式（错误原文、crash report 路径）
2. 已知 Bug 库摘要（state_read("known_bugs") 先查，避免重复修）
3. 期望产出：根因 → 修复（走 worktree 签名提交）→ 验证前后对比 → 落账清单
4. 边界：根因不明不动手；修不了就上报已排除假设

子代理返回后（主 Agent 落账）：state_update(known_bugs) + remember(kind="bug")。
