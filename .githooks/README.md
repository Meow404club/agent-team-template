# Git 钩子（GPG 双层强制）

| 钩子 | 时机 | 作用 |
|---|---|---|
| `guard-commit.sh` | ZCode PreToolUse(Bash) | 裸 `git commit`（无 `-S`）在 AI 发起时就地拦截，deny 并提示改用 `git commit -S -s` |
| `pre-commit` | git commit | 拦截 tmp/、tools/.venv/、tools/config.json（密钥）、>5MB 文件、疑似 `sk-` API 密钥 |
| `commit-msg` | git commit | 校验消息格式 `<type>(<scope>)?: <主题>`（type 为小写词，词表不限）+ `Signed-off-by:`/`Task:` 标签 |
| `pre-push` | git push | 全量 `git verify-commit`，任何无有效 GPG 签名的提交禁止推送 |

## 安装

```bash
git config core.hooksPath .githooks     # scripts/setup.sh 会自动做
```

## ZCode PreToolUse 钩子注册（二选一）

`guard-commit.sh` 自带作用域自检（只对存在 `.githooks/commit-msg` 的仓库 +
`../<仓库名>-trees/` 目录生效），两种注册方式行为一致：

**方式 A · 用户级（推荐，免信任审核弹卡）** —— 编辑 `~/.zcode/cli/config.json`：

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "PreToolUse": [
        { "matcher": "Bash",
          "hooks": [ { "type": "process", "command": "<本仓库绝对路径>/.githooks/guard-commit.sh", "timeoutMs": 10000 } ] }
      ]
    }
  }
}
```

**方式 B · 工作区级** —— 在 `.zcode/config.json` 顶层加同样结构的 `hooks` 段。
首次触发时 ZCode 会弹一次工作区钩子信任审核卡（确认后持久化）。
