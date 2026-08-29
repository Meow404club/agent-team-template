---
description: 派发审查合并官子代理（review-merge）：审查 work/* 分支（GPG/红线/语义/编译），裁决合入 main 或打回。main 唯一写入口；可单会话批量传入多个分支顺序处理。
---
用 Agent 工具派发 `subagent_type: review-merge`。

任务输入：$ARGUMENTS（work/<slug> 分支名，**单个或多个**（空格分隔）+ 可选审查重点；
多个并行完成的分支尽量合并到一次派发，由同一会话顺序处理）

派发 prompt 必须自包含，至少包含：
1. 仓库根绝对路径 + 待审分支列表（每支对应任务卡要点 SPEC/EVIDENCE/FILES_SCOPE）
2. 其他在途分支的 files_scope（并行冲突裁决依据）
3. 期望产出：每分支一段 VERDICT + MERGED hash + ISSUES

子代理返回后（主 Agent 落账）：state_update(tasks 各分支状态/merged_commit)
+ remember(kind="merge") + 通知其余在途 coder 分支 rebase main。
