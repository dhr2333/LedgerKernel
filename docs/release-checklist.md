# Release Checklist

适用于 LedgerKernel 的任何版本发布。安全相关版本（审计、批准、提交、恢复、迁移）必须先有 ADR，并完成一次真实账本副本回归。

## 1. 发布前

- [ ] `main` 最新，工作区干净，Jenkins `main` 构建为绿（commit check `LedgerKernel CI`；配置见 [ci-github-webhook](ci-github-webhook.md)）。
- [ ] `PYTHONPATH=src python3 -m unittest discover -s tests -t .` 全部通过。
- [ ] 涉及写入/恢复/外部变化的改动已跑 `PYTHONPATH=src python3 scripts/e2e_testassets.py --source <TestAssets>`。
- [ ] 在至少一份真实账本副本上完成：提交、外部修改、旧草稿失效、恢复/拒绝提交。
- [ ] Beancount 版本范围未变；如变更，已跑真实账本回归并更新 engine fingerprint 说明。
- [ ] 版本号同时更新：`pyproject.toml`、`src/ledger_kernel/__init__.py`。
- [ ] `CHANGELOG.md` 增加本次版本条目，写清新增、修复、破坏性变化与迁移方式。
- [ ] 新增/变更的安全边界已写入 ADR，并在 README/`AGENTS.md` 中有对应说明。
- [ ] 确认没有提交真实账本、`.ledger-kernel/`、token、私钥或个人信息。
- [ ] 备份/导出路径仍可用；`.ledger-kernel` 中草稿、审计、journal 仍是可读文件。

## 2. 打标签与发布

```bash
git tag -s vX.Y.Z -m "LedgerKernel vX.Y.Z"
git push origin main --tags
```

- [ ] 从 tag 重新安装并做一次冷启动：`pip install -e .` + `ledger-kernel --version` + `init/status`。
- [ ] 在 GitHub 创建 Release，粘贴 CHANGELOG 对应段落与升级说明。
- [ ] 若存在数据格式/配置变更，写明迁移步骤和回滚方式。
- [ ] 通知使用方；私有仓库仅通知授权成员。

## 3. 发布后

- [ ] 观察 Jenkins `main` 下一次构建（webhook 触发，通常在推送后 1 分钟内出现）；失败则回滚 tag 或立即修复。
- [ ] 用真实账本副本再提交一笔交易，确认审计、journal、快照正常。
- [ ] 记录本次发布中的问题，进入下一轮待办。

## 4. 回滚

- 代码回滚：`git revert` 或部署上一个 tag，不使用 `reset` 改写已发布历史。
- 账本回滚：只能使用提交前快照/备份恢复**技术故障**；已入账业务事实的修正必须走冲销草稿，不能把文件恢复当会计冲销。
- 若出现 `RecoveryRequired`：停止新提交，先检查 journal 与目标文件 hash，再执行 `recovery recover`；不确定时保留现场，不手工拼接账本。

## 5. 版本兼容性记录

每次发布在 Release notes 中记录：

- 支持的 Python / Beancount 范围；
- `engine_fingerprint` 是否变化；
- 草稿 schema、契约 schema、`.ledger-kernel` 布局是否变化；
- 旧 validation / approval / journal 是否仍可读取。
