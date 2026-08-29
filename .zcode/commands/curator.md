---
description: 派发资料策展人子代理（curator）：审查 tmp/harvest 收割资料，剔除噪声子目录，裁决入 RAG 并触发增量索引+检索验证。
---
用 Agent 工具派发 `subagent_type: curator`。

任务输入：$ARGUMENTS（待审资料名列表或"全部积压"；附调研背景一句话）

派发 prompt 必须自包含，至少包含：
1. 待审的 harvest 资料名（tmp/harvest/<name>/）+ 对应研究卡结论/调研背景
2. 期望产出：每资料裁决（index/partial/skip/removed + exclude globs）+
   refresh_index(source="harvest") 触发 + 检索验证 + state_update(harvest_log)

子代理返回后（主 Agent 落账）：核对 state 的 harvest_log 已更新；把裁决结果
回链到对应研究卡/任务板条目。
