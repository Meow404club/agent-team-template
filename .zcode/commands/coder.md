---
description: 派发蓝领码农子代理（coder）：独立 worktree 内按任务卡实现，GPG 签名原子提交，返还 commit hash。可并行多个不同 slug。
---
用 Agent 工具派发 `subagent_type: coder`（可并行 2~4 个，文件域必须互不重叠）。

任务输入：$ARGUMENTS

派发 prompt 必须含完整任务卡（子代理不加载本项目 AGENTS.md）：

```
SLUG: <kebab-case 唯一名>
SPEC: 做什么、不做什么（边界写死）
EVIDENCE: 已求证结论与 文件:行号（researcher 的产出直接粘入）
FILES_SCOPE: 允许触碰的文件/目录（并行隔离的关键）
ACCEPTANCE: 可验证的完成标准（编译通过/测试/具体行为）
BRANCH: work/<SLUG>（worktree ../<仓库名>-trees/<SLUG> 由 coder 自建）
```

子代理返回 COMMITS 后：登记 state_update(key="tasks", value={"<slug>":{"status":"in_review","commits":[...]}}, merge=true)，
立即派发 review-merge。
