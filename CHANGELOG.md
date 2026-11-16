# Changelog

所有重要变更记录在此。格式参考 Keep a Changelog；版本遵循语义化版本。

## [Unreleased]

### Changed

- CI 触发方式明确为 GitHub webhook → 内部 Jenkins Multibranch；构建结果回写为 commit check `LedgerKernel CI`，配置步骤见 `docs/ci-github-webhook.md`。

## [0.1.0] - 2026-11-15

### Added

- Beancount Adapter：官方 `loader.load_file()` 加载、include 闭包、账户树/余额、revision manifest、engine fingerprint。
- Revision Observer：外部变化检测、内容寻址快照、diff、`ExternalChangeObserved`、旧草稿失效、无效账本阻断。
- 结构化草稿：不可变 JSON schema、idempotency、证据引用、文本安全规则。
- 完整 Dry-run：临时工作区全账本加载、ADR-012 增量断言、options/插件/其他文件一致性检查、余额与文件 diff、review digest。
- 声明式账户契约：硬阻断与警告分级、未知字段严格校验。
- 人工批准 + 受控提交：单 `write_target` 追加、写前快照、journal、二次校验、最终 revision 复查、原子替换、崩溃恢复。
- 哈希链审计、外部变化观测、`ApprovalCarriedForward`、`corrections propose` 冲销草稿。
- CLI 审核闭环与只读/提案型 MCP stdio 适配器。
- 55 项单元/集成测试与 TestAssets 真实账本副本 E2E 脚本。

### Known limitations

- 仅支持单文件追加、单活跃写入目标；不做多文件事务或多设备多主写入。
- POSIX `flock`；Windows 需替换锁实现。
- 外部来源固定 `external_unknown`，未接入 Git/OS audit 佐证。
- 余额 diff 不做成本/价格换算；完整语义由 Beancount 校验。
