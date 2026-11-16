# Changelog

所有重要变更记录在此。格式参考 Keep a Changelog；版本遵循语义化版本。

## [Unreleased]

### Added

- 账户层 dogfood 协议升级到 v0.2.x 轮次（`docs/dogfood.md` / `.en.md`）：新增账户层指标、每轮记录表与判据 #6–#9；新增基线工具 `scripts/dogfood_account_layer.py`（合成 fixture 可复现，`--source` 真实账本临时副本只读）。
- `accounts list` 每行新增 `assertion_status` / `last_assertion` / `reconciliation`（与 `accounts show` 一致）；`review --by-account` 改用一次批量读取。
- v0.2 账户层 dogfood 基线轮报告 `docs/design/v0.2-dogfood-round2.md`：真实账本副本 97 个活跃账户、17/17 检查通过、不含真实数据。

### Changed

- 读与元数据操作在闭包字节未变时复用上次 Beancount 加载结果（每次仍重算闭包哈希），不再重复全量解析；对账记录增加按审计文件签名的进程内缓存。真实账本副本：`get_account` p50 929→34ms、`reconcile_account` p50 912→31ms、`review --by-account` 中位 4840→878ms；`validate` 仍为完整 dry-run（p50 ≈1.14s/笔），留作后续性能项。

## [0.2.0] - 2026-11-16

### Added

- 合成 fixture 账本 `tests/fixtures/ledger`：多层 include、插件、pad/balance 断言、已关闭账户与固定 `write_target`，不含真实数据。
- 真实账本 dogfood 协议 `docs/dogfood.md`：样本、指标与 v0.2 排序判据。
- v0.2 范围建议 `docs/design/v0.2-scope.md`：4 周真实账本 dogfood 的五项指标、P0 缺陷与候选排序（2026-11-16 冻结，P0/P1 已落地）。
- 最小审核入口 `ledger-kernel review`：待审队列、逐笔片段/检查项、`--approve --confirm` 批量批准。
- 草稿模板与提交入口：`.ledger-kernel/templates.yaml`、`ledger-kernel templates list|show`、`ledger-kernel drafts submit --template|--payload`（ADR-016）。
- validate 新增建议性 `DUPLICATE_SUSPECTED` 告警：重复流水可见但不阻断（ADR-017）。
- ADR-014（engine fingerprint 不绑定 Python）、ADR-015（review digest 只覆盖交易正文）、ADR-016（草稿模板）、ADR-017（重复告警）。
- v0.2 账户层设计 `docs/design/v0.2-account-layer.md` 与 ADR-018/019/020：契约编辑、账户状态/对账、账户级审核（W1-W3 已实现并随 v0.2.0 发布）。
- `AGENTS.md` 明确发布前设计变更约定：正式发布前允许推翻旧设计，不要求向后兼容；该约定不写成 SKILL。
- 账户层设计评审记录：对账不要求证据；对账永不阻断写入，`hard_deadline` 兼容读取但忽略；支持可选申报余额差异（只读）。
- v0.2 账户层实施计划 `docs/design/v0.2-account-layer-plan.md`：工作包、文件映射、测试矩阵与实施顺序（W1-W4 已实现，实施结果见第 9 节）。
- 端到端脚本 `scripts/e2e_ledger.py` 覆盖提交、外部修改观测与 diff、草稿失效、无效账本阻断；CI 每次构建执行，并有一条 `unittest` 集成用例。
- 契约编辑入口 `contracts show/validate/diff/apply`（v0.2 W1）：双哈希 CAS、严格预校验、原子替换、`ContractChanged` 审计与前后内容 blob；冲突返回新增错误码 `CONTRACT_CONFLICT` 且文件不变（ADR-019）。
- 契约 schema 在 `schema_version: 1` 内加法新增可选 `unmanaged_accounts`：与 `accounts` 不允许重叠；契约覆盖率与 `unmanaged_accounts` 计数通过 `accounts list --coverage` 展示。
- 账户对账入口 `accounts reconcile` 与 `accounts reconciliation list|revert`（v0.2 W2）：`AccountReconciled` / `AccountReconciliationReverted` 追加式审计，按契约 `cadence` 推导 `not_configured/never/current/overdue` 与 `next_due`，可选申报余额与只读差异，新错误码 `RECONCILIATION_INVALID`；`accounts show` 增加 `assertion_status`/`last_assertion` 摘要与 `reconciliation` 视图，`state.json.reconciliations` 可从审计重建。
- 账户级审核分组视图 `review --by-account` / `review --account <name>`（v0.2 W3）：只读按账户聚合待审草稿，展示生命周期、余额、契约摘要、对账状态与草稿 latest validation 中的 `CONTRACT_*` 警告；`--account` 无草稿也显示该组，未知账户返回 `groups: []` 与 `note`。不新增账户级批准对象、审计事件或状态机状态；`--approve` 仍要求显式 draft_id 与 `--confirm`，逐 draft 绑定 `validation_id/ledger_revision/contract_revision`（ADR-020）。
- v0.2 账户层验收矩阵：README 新增 A01～A14 用例映射；设计与实施计划标记为已实现，实施结果（146 个测试、合成与真实账本副本 e2e 全绿、提交记录）记录在 `v0.2-account-layer-plan.md` 第 9 节。

### Changed

- README 标题不再限定 v0.1，补充 v0.2 账户层冻结决策、验收矩阵与已知限制；设计文档索引与文档状态更新为“v0.1 原型 + v0.2 账户层（v0.2.0）”。
- 契约 revision 变化时显式失效所有 pending validation/approval，下一次操作自动重验；契约编辑不写 Ledger、不改变 `ledger_revision`。新建内核的默认 `contracts.yaml` 增加 `unmanaged_accounts: []` 与说明注释，旧契约文件无需改动即可加载。
- `engine_fingerprint` 只包含 Beancount 与 ledger-kernel 版本，不再包含 Python 运行时（ADR-014）：审核端换解释器不再使全部 validation 失效。
- `review_digest` 改为哈希渲染后的交易正文，不再包含追加分隔符（ADR-015）：空 `write_target` 首笔提交后，其余已批准草稿可继续沿用批准。
- v0.2 范围修订 1：回到“账户是产品核心”，契约编辑、账户状态/对账、账户级审核升为 P1；导入对账、批量性能、metadata 降为支撑项。
- 明确长期定位：独立本地账户内核，不计划与 Beancount-Trans 或特定产品集成；目标是让个人拥有可读、可导出、可带走的账本。
- `scripts/e2e_testassets.py` 重命名为 `scripts/e2e_ledger.py`：默认跑合成 fixture，仍可用 `--source <TestAssets>` 跑真实账本副本。
- CI 触发方式明确为 GitHub webhook → 内部 Jenkins Multibranch；构建结果回写为 commit status（context `continuous-integration/jenkins/branch`），配置步骤见 `docs/ci-github-webhook.md`。
- 新增维护者本地的 Jenkins 令牌轮换约定（`jk-token rotate`，失败关闭、先验证后吊销），凭据不进入仓库。
- `CONTRACT_RECONCILIATION` 判据从“最后记账日超过 cadence”改为“对账状态为 `never`/`overdue` 才警告”，仍是 warning、永不阻断；对账/撤销不写 Ledger、不改变 `ledger_revision`，`hard_deadline` 兼容读取但忽略；MCP 不暴露对账写入口。

### Fixed

- 审核端 Python 版本不同触发 `EngineChanged`、9 篇草稿全部失效且批准静默失败。
- 空 `write_target` 首笔提交后其余草稿 `review_digest` 变化，批量批准只兑现第 1 笔。

#### 兼容性记录

- Python ≥3.11；Beancount `>=3.2,<4`（验证于 3.2.3）。
- `engine_fingerprint` = `beancount-<版本>;ledger-kernel-<版本>`（ADR-014，不含 Python 运行时）；升级到 0.2.0 会使其变化：首次操作记录 `EngineChanged`，pending validation/approval 失效，账本文件与审计链不变，重验即可。
- 契约 schema 仍为 `1`，新增可选 `unmanaged_accounts`；旧内核会因严格未知字段检查拒绝该字段，所有内核实例须同版本使用。
- `state.json` 新增 `reconciliations` 缓存：缺失或损坏时从审计链重建，无需手工迁移。
- 新增命令 `contracts show/validate/diff/apply`、`accounts reconcile`、`accounts reconciliation list/revert`、`review --by-account/--account`、`accounts list --coverage`；新增错误码 `CONTRACT_CONFLICT`、`RECONCILIATION_INVALID`。
- 草稿 schema、journal、validation、observation 记录格式不变。

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
