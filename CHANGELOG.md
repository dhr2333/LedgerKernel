# Changelog

所有重要变更记录在此。格式参考 Keep a Changelog；版本遵循语义化版本。

## [Unreleased]

### Added

- 合成 fixture 账本 `tests/fixtures/ledger`：多层 include、插件、pad/balance 断言、已关闭账户与固定 `write_target`，不含真实数据。
- 真实账本 dogfood 协议 `docs/dogfood.md`：样本、指标与 v0.2 排序判据。
- v0.2 范围建议 `docs/design/v0.2-scope.md`：4 周真实账本 dogfood 的五项指标、P0 缺陷与候选排序（2026-11-16 冻结，P0/P1 已落地）。
- 最小审核入口 `ledger-kernel review`：待审队列、逐笔片段/检查项、`--approve --confirm` 批量批准。
- 草稿模板与提交入口：`.ledger-kernel/templates.yaml`、`ledger-kernel templates list|show`、`ledger-kernel drafts submit --template|--payload`（ADR-016）。
- validate 新增建议性 `DUPLICATE_SUSPECTED` 告警：重复流水可见但不阻断（ADR-017）。
- ADR-014（engine fingerprint 不绑定 Python）、ADR-015（review digest 只覆盖交易正文）、ADR-016（草稿模板）、ADR-017（重复告警）。
- v0.2 账户层设计草案 `docs/design/v0.2-account-layer.md` 与 ADR-018/019/020：契约编辑、账户状态/对账、账户级审核（设计先行，未实现）。
- 端到端脚本 `scripts/e2e_ledger.py` 覆盖提交、外部修改观测与 diff、草稿失效、无效账本阻断；CI 每次构建执行，并有一条 `unittest` 集成用例。

### Changed

- `engine_fingerprint` 只包含 Beancount 与 ledger-kernel 版本，不再包含 Python 运行时（ADR-014）：审核端换解释器不再使全部 validation 失效。
- `review_digest` 改为哈希渲染后的交易正文，不再包含追加分隔符（ADR-015）：空 `write_target` 首笔提交后，其余已批准草稿可继续沿用批准。
- v0.2 范围修订 1：回到“账户是产品核心”，契约编辑、账户状态/对账、账户级审核升为 P1；导入对账、批量性能、metadata 降为支撑项。
- 明确长期定位：独立本地账户内核，不计划与 Beancount-Trans 或特定产品集成；目标是让个人拥有可读、可导出、可带走的账本。
- `scripts/e2e_testassets.py` 重命名为 `scripts/e2e_ledger.py`：默认跑合成 fixture，仍可用 `--source <TestAssets>` 跑真实账本副本。
- CI 触发方式明确为 GitHub webhook → 内部 Jenkins Multibranch；构建结果回写为 commit status（context `continuous-integration/jenkins/branch`），配置步骤见 `docs/ci-github-webhook.md`。
- 新增维护者本地的 Jenkins 令牌轮换约定（`jk-token rotate`，失败关闭、先验证后吊销），凭据不进入仓库。

### Fixed

- 审核端 Python 版本不同触发 `EngineChanged`、9 篇草稿全部失效且批准静默失败。
- 空 `write_target` 首笔提交后其余草稿 `review_digest` 变化，批量批准只兑现第 1 笔。

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

#### 兼容性记录

- Python ≥3.11；Beancount `>=3.2,<4`（验证于 3.2.3）。
- `engine_fingerprint` = `beancount-<版本>;python-<版本>;ledger-kernel-<版本>`，三者任一变化都会改变它，并计入每次 ledger revision。
- 草稿 schema `1`、契约 schema `1`；`.ledger-kernel` 的 state/journal/validation/observation 记录均为 `schema_version: 1`。
- 首次发布：不存在更早的草稿/validation/journal 数据，无需迁移。

### Known limitations

- 仅支持单文件追加、单活跃写入目标；不做多文件事务或多设备多主写入。
- POSIX `flock`；Windows 需替换锁实现。
- 外部来源固定 `external_unknown`，未接入 Git/OS audit 佐证。
- 余额 diff 不做成本/价格换算；完整语义由 Beancount 校验。
