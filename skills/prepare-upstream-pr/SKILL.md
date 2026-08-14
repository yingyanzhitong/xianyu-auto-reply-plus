---
name: prepare-upstream-pr
description: 从指定功能点、文件或提交中提炼最小改动，基于 base/main 在 fork 创建主题分支并发起正式上游 PR。用于用户要求将某个功能或 commit 贡献到上游、避免混入开发仓库改动，或需要将草稿 PR 转为正式 PR 的场景。
---

# 基于 base/main 在 fork 发正式上游 PR

将一个明确功能点或提交中的最小必要改动贡献到上游。默认远程约定如下：

- `origin`：独立开发仓库 `yingyanzhitong/xianyu-auto-reply-plus`，仅作为功能、提交或文件的来源。
- `fork`：专用 PR fork `yingyanzhitong/xianyu-auto-reply`，所有上游 PR 分支必须在此创建并推送。
- `base`：上游仓库 `zhinianboke/xianyu-auto-reply`，PR 的目标为 `base/main`。

在本流程中，禁止向 `origin` 或 `base` 推送 PR 分支。

## 先确定范围

- 要求用户给出功能点、提交 SHA、文件或现有分支之一。范围仍不明确时，先询问，不要猜选其他改动。
- 读取项目的 `AGENTS.md` 和提交规则，检查 `git status -sb`。工作区含有无关改动时，不要执行 `git add -A` 或覆盖它们。
- 检查远程与基线：

```bash
git remote -v
git fetch base main
git fetch fork main
git fetch origin --prune
git log -1 --oneline base/main
```

- 确认 `fork` 和 `base` 的地址分别为 `yingyanzhitong/xianyu-auto-reply` 与 `zhinianboke/xianyu-auto-reply`。远程名称或地址不符合约定时，先停止并说明问题。
- 候选功能可以来自 `origin` 的分支、提交或当前工作区，但 `origin` 只提供来源，不承载上游 PR 分支。

- 查询同类开放 PR，避免重复提交：

```bash
gh pr list --repo <upstream-owner>/<repo> --base main --state open
```

## 提取最小补丁

提交不是天然的功能边界。先审查候选提交，再决定是否可直接复用：

```bash
git show --stat <commit>
git diff <commit>^ <commit> -- <候选路径>
```

- 提交只含目标功能时，可以采用等效补丁或谨慎 cherry-pick。
- 提交混有订单、页面、发布、配置或其他功能时，只手工补入目标代码和相应测试；不要整体 cherry-pick。
- 每一处变更都必须能追溯到用户选定的功能点。保留上游已有代码风格和行为。

## 先在 fork 创建 PR 分支

先以 `base/main` 为起点在 `fork` 创建远程主题分支，再创建同名本地分支。不得从 `origin/main`、发布分支或既有 PR 分支切出：

```bash
git push fork base/main:refs/heads/agent/<short-feature-name>
git switch -c agent/<short-feature-name> base/main
git branch --set-upstream-to=fork/agent/<short-feature-name> agent/<short-feature-name>
git merge-base --is-ancestor base/main HEAD
```

- 创建前检查 `fork` 中不存在同名分支；若已存在，改用新的分支名或先与用户确认，绝不覆盖已有分支。
- 上述首次 `git push` 的目的仅是从上游基线在 `yingyanzhitong/xianyu-auto-reply` 切出分支；之后所有分支更新同样只推送到 `fork`。
- 若当前工作区不能安全切换，使用独立 worktree；不要清理、还原或暂存用户的无关改动。

## 实现与验证

- 添加针对根因的最小回归测试，优先复用项目现有测试框架。
- 运行与改动相称的测试、语法检查或构建，并执行 `git diff --check`。
- 检查完整上游差异，而非只看最后一次提交：

```bash
git diff --stat base/main...HEAD
git diff --check base/main...HEAD
```

- 遵从项目提交规则：如要求版本和中文 `CHANGELOG.md`，只递增所属组件版本；如要求标签，确认标签未占用后推送到 fork。不要因为创建上游 PR 擅自部署生产环境。

## 提交、推送到 fork 与正式 PR

- 仅暂存已核对的路径，使用简短且准确的提交信息。
- 将新分支更新推送到 `fork`，不得推送到 `origin` 或 `base`：

```bash
git push fork HEAD:refs/heads/agent/<short-feature-name>
```

- 创建跨 fork PR 时，明确指定上游仓库、目标分支与 fork 头分支。创建正式 PR，**不得添加 `--draft`**：

```bash
gh pr create \
  --repo zhinianboke/xianyu-auto-reply \
  --base main \
  --head yingyanzhitong:agent/<short-feature-name> \
  --title '<类型>: <简短说明>' \
  --body-file <pr-body.md>
```

- PR 描述至少包括：变更内容、根因、用户影响、明确不涉及的范围、验证命令与结果。若断连或重启可能丢消息，写明可恢复的后续消息与不可恢复的断连窗口。
- 若 PR 已存在且为草稿，执行 `gh pr ready <number> --repo <upstream-owner>/<repo>`，不要重建 PR。

## 交付核验

回读 PR，确认它是打开的正式上游 PR：

```bash
gh pr view <number> --repo <upstream-owner>/<repo> \
  --json url,state,isDraft,baseRefName,headRefName,commits,changedFiles
```

完成条件：`state` 为 `OPEN`、`isDraft` 为 `false`、`baseRefName` 为 `main`，且变更与 `base/main...HEAD` 的审查结果一致。报告 PR 链接、分支、提交、验证结果和未验证边界。
