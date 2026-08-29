---
description: 派发研究员子代理（researcher）：代码考古 / 联网调研（论文/文档/开源项目）/ 方案对比选型，产出带分层证据链的研究卡。
---
用 Agent 工具派发 `subagent_type: researcher`。

任务输入：$ARGUMENTS（研究问题要具体、单一；联网能力无需额外授权——子代理有
WebSearch/WebFetch，且 brain MCP 的 `web_fetch` 工具可过反爬抓正文）

派发 prompt 必须自包含，至少包含：
1. 研究问题（一句可判完成的话）+ 建议调研模式（A 代码考古 / B 外部调研 /
   C 方案选型；不确定就留给子代理自选）
2. 已知线索：先 recall/kg_search 查过的结论直接贴入，避免重复调研
3. 期望产出：研究卡（模式 / 分层证据（本地 文件:行号；网络 URL+访问日期）/
   结论（选型题=推荐+理由）/ 风险）
4. 落账要求：remember(kind="research")；选型结论加 state_update(decisions)；
   有实体关系加 kg_add

子代理返回后（主 Agent 落账）：state_update(decisions) + 把研究卡编号挂到任务板。
