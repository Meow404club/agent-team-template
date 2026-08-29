---
description: 派发审查合并官子代理（review-merge）：审查 work/* 分支（GPG/红线/语义/编译），裁决合入 main 或打回。main 唯一写入口，串行使用。
---
用 Agent 工具派发 `subagent_type: review-merge`（一次只派一个，main 是全局锁）。

任务输入：$ARGUMENTS（work/<slug> 分支名 + 可选审查重点）

派发 prompt 必须自包含，至少包含：
1. 仓库根绝对路径 + 要审的分支与对应任务卡要点（SPEC/EVIDENCE/FILES_SCOPE）
2. 其他在途分支的 files_scope（并行冲突裁决依据）
3. 期望产出：VERDICT + MERGED hash + ISSUES

子代理返回后（主 Agent 落账）：state_update(tasks 状态/merged_commit) + remember(kind="merge")
+ 通知其余在途 coder 分支 rebase main。
