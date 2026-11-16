# LedgerKernel

本地账本账户内核：围绕 Beancount 账本提供**提案 → 完整 Dry-run → 人工确认 → 受控追加 → 审计 → 恢复**的闭环。

账本文件是财务事实的唯一真相源；内核是所有**自动化写入**的唯一受控入口。用户保留直接编辑账本的最高权限，内核不阻止、不覆盖，只负责发现、记录，并在无法安全归因时明确标记 `external_unknown`。

> 本仓库是 v0.1 原型实现，自 **v0.2.0（2026-11-16）** 起提供 v0.2 账户层（契约编辑、账户对账、账户级审核）。设计基线——顶层设计摘要、工程决策冻结与原型实施计划、设计规格、实施记录、v0.2 账户层设计与 ADR——以 [`docs/design/`](docs/design/README.md) 为唯一真相源；库外笔记不保留副本。实现覆盖 P0-A～P0-D 及薄 MCP 适配器（P0-E 的最小工具集）。

CI：Jenkins（内部，GitHub webhook 触发，配置见 [ci-github-webhook](docs/ci-github-webhook.md)）· 开发约定：[CONTRIBUTING.md](CONTRIBUTING.md) · Agent 约定：[AGENTS.md](AGENTS.md) · 发布流程：[release-checklist](docs/release-checklist.md) · 版本记录：[CHANGELOG.md](CHANGELOG.md)

## 不是什么

- 不是记账 App、预算工具、消费分析仪表盘。
- 不绑定特定前端或产品；CLI/MCP/Python API 是通用入口。目标是让用户拥有可读、可导出、可带走的账本，而不是成为某个应用的写入后端。
- 不内置 AI 推理，不绑定模型；Agent 在进程外，只提交结构化草稿。
- 不承诺“Agent 永远无法直写账本”。同一 OS 用户下无法可靠区分 Agent 与用户；直写按外部变化处理。
- 不把 Git 历史当审计，不把 hash 当身份认证。
- v0.1 不做自动审核分级：所有草稿必须人工批准。

## 快速开始

要求 Python ≥ 3.11、Beancount 3.2.x。

```bash
git clone git@github.com:dhr2333/LedgerKernel.git
cd LedgerKernel
python3 -m pip install -e .
# 或者不安装，使用 PYTHONPATH=src

ledger-kernel init /path/to/ledger/main.bean \
  --write-target transactions/kernel-import.bean

ledger-kernel status
ledger-kernel accounts list
ledger-kernel accounts show Assets:Cash
ledger-kernel accounts balance Assets:Cash --as-of 2026-11-15
```

`write_target` 必须：

- 是账本目录内的规范化相对路径；
- 真实存在且不是符号链接；
- 被根账本的 `include` 闭包实际加载；
- 使用 UTF-8（可带 BOM），换行风格在追加时保留。

`init` 会创建 `.ledger-kernel/` 并写入初始 revision、审计链头与空契约文件。若目录已绑定，拒绝覆盖。

## 提案与审核闭环

Agent 只提交结构化 JSON，不提交 Beancount 原文。示例：

```json
{
  "schema_version": 1,
  "operation": "append_transactions",
  "transactions": [
    {
      "date": "2026-11-15",
      "flag": "*",
      "payee": "Example Bookstore",
      "narration": "Books",
      "tags": ["reimbursement"],
      "postings": [
        {"account": "Expenses:Books", "units": "120.00", "currency": "CNY"},
        {"account": "Assets:Bank:Checking", "units": "-120.00", "currency": "CNY"}
      ]
    }
  ],
  "evidence": [
    {"kind": "file", "locator": "documents/2026-11/receipt.pdf", "sha256": "..."}
  ],
  "confidence": 0.91,
  "rationale_summary": "依据收据商户、金额和日期生成。"
}
```

字段规则：金额必须是十进制字符串（拒绝 JSON float）；每个 posting 必须显式给出金额；`payee`/`narration` 拒绝换行、控制字符、方向覆盖字符；account/currency/tag/link 按 Beancount 语法约束；成本、价格、metadata 不在 v0.1 schema 内。

审核流程：

```bash
# Agent 通过 MCP 或 Python API 提交；CLI 也可以直接调用内核 API
ledger-kernel drafts list
ledger-kernel drafts show drf_...
ledger-kernel drafts validate drf_...        # 返回 validation_id、检查项与 diff
ledger-kernel drafts diff drf_... --validation val_...
ledger-kernel drafts approve drf_... --confirm drf_...
ledger-kernel drafts commit drf_...
ledger-kernel corrections propose cmt_...   # 生成冲销草稿，仍需 validate/approve/commit
```

最小审核入口与草稿模板：

```bash
ledger-kernel review                          # 待审队列：状态、账户、金额、校验告警
ledger-kernel review drf_... drf_...          # 逐笔片段、检查项与 diff
ledger-kernel review drf_... --approve --confirm drf_...
ledger-kernel templates list
ledger-kernel drafts submit --template electricity --amount 99.90 --date 2026-11-15
ledger-kernel drafts submit --payload draft.json
```

```yaml
# .ledger-kernel/templates.yaml
schema_version: 1
templates:
  electricity:
    payee: Electric Utility
    narration: Monthly power
    tags: [Family, Fixed]
    postings:
      - {account: Expenses:Home:Utilities, units: amount}
      - {account: Liabilities:CreditCard:Bank:CMB:C0002, units: -amount}
```

模板只预填重复字段，展开后仍是普通草稿，必须 validate/approve/commit；不绕过审核。validate 对重复流水给出 `DUPLICATE_SUSPECTED` 告警（不阻断），关闭订单与退款配对仍由导入端判断。

批准必须显式确认（`--confirm` 的值必须等于草稿 ID；批量时用逗号分隔的完整列表），非交互模式不会因为“调用了命令”而自动批准。批准绑定 `draft_id + validation_id + ledger_revision + contract_revision`，默认 30 分钟过期。

## MCP（Agent 适配器）

```bash
ledger-kernel-mcp --ledger /path/to/ledger
```

stdio 传输，一行一个 JSON-RPC 消息。暴露：

- `ledger_get_status`
- `ledger_list_accounts`
- `ledger_get_account`
- `ledger_get_balance`
- `ledger_submit_draft`
- `ledger_get_draft_status`

**不暴露**批准、提交、写文件、修改契约、对账的工具。MCP 层不重新实现校验或写入逻辑，只调用 `Kernel` 用例。

Claude Desktop / DSH 等客户端配置示例：

```json
{
  "mcpServers": {
    "ledger-kernel": {
      "command": "ledger-kernel-mcp",
      "args": ["--ledger", "/path/to/ledger"]
    }
  }
}
```

## 安全不变量

1. **结构化提案**：Agent 不能提交原始 Beancount 文本；文本由内核渲染，转义 `"` 与 `\`，拒绝控制字符。
2. **完整 Dry-run**：把 include 闭包复制到临时工作区，只替换 `write_target`，用同一版本 Beancount 完整加载；非账本附件目录以符号链接镜像，避免 Document 指令误报。
3. **ADR-012 增量断言**：旧 entries 必须全部仍在；新增 entries 数量等于提案交易数，且逐项与提案相等；`options_map` 与插件列表不变；闭包内其他文件字节不变。任何不符即 `DELTA_MISMATCH`。
4. **片段指纹**：validation 保存实际待追加字节（含必要的分隔换行）的 SHA-256；提交时重新渲染并比对，同时校验已存片段自身哈希。不一致即 `FRAGMENT_MISMATCH`。
5. **人工批准**：批准绑定 validation 与两个 revision；Agent 无法调用批准接口。
6. **受控提交**：单文件、只追加；提交前二次完整校验、写前快照、`PREPARED` journal、目标 hash 复查、最终整账本 revision 复查、同目录临时文件 `fsync` + `os.replace` + 目录 `fsync`、`COMMITTED` 审计。
7. **崩溃恢复**：重启后按 journal 的 before/after hash 判定“已应用/未应用/不确定”；已应用且账本 revision 等于 journal 记录才确认提交，绝不重复追加；不确定进入 `RecoveryRequired`，等待人工处理。
8. **外部变化**：每次操作前重算闭包指纹；发现变化时重新加载、保存 diff（有旧 blob 时）、失效旧草稿、记录 `ExternalChangeObserved`。外部变化导致账本无效时进入 `LedgerInvalid`，阻断受控提交，但不改用户文件。闭包字节未变时复用上次 Beancount 加载结果（仍每次重算闭包哈希），不跳过外部变化检测。
9. **已知转移**：只有“当前 revision = 旧 manifest 仅替换 `write_target` 为某条 COMMITTED journal 的 after_hash”才视为内核自身提交；其他一律按外部变化。批量批准可在 `review_digest` 不变时沿用批准（`ApprovalCarriedForward`），不跳过重验。
10. **审计链**：`events.jsonl` 每条事件带 `prev_hash` 与 `event_hash`；链损坏/为空时拒绝提交（`AUDIT_INTEGRITY`）。

## 设计文档

- [设计文档索引](docs/design/README.md)
- [顶层设计摘要](docs/design/design-summary.md)
- [v0.1 设计规格](docs/design/v0.1-spec.md)
- [v0.1 工程决策冻结与原型实施计划](docs/design/v0.1-implementation-plan.md)
- [v0.1 实施完成记录](docs/design/v0.1-implementation-report.md)
- [v0.2 范围建议](docs/design/v0.2-scope.md)
- [v0.2 账户层设计](docs/design/v0.2-account-layer.md)
- [v0.2 账户层实施计划](docs/design/v0.2-account-layer-plan.md)（实施结果见第 9 节）
- ADR：[ADR-012 增量断言](docs/design/adr/ADR-012-delta-assertion.md)、[ADR-013 已知转移](docs/design/adr/ADR-013-known-transition.md)、[ADR-014 engine 指纹](docs/design/adr/ADR-014-engine-fingerprint.md)、[ADR-015 review digest](docs/design/adr/ADR-015-review-digest.md)、[ADR-016 草稿模板](docs/design/adr/ADR-016-draft-templates.md)、[ADR-017 重复告警](docs/design/adr/ADR-017-duplicate-warning.md)
- v0.2 ADR：[ADR-018 对账是工作流事实](docs/design/adr/ADR-018-reconciliation-fact.md)、[ADR-019 契约编辑](docs/design/adr/ADR-019-contract-apply.md)、[ADR-020 账户级审核](docs/design/adr/ADR-020-account-level-review.md)

这些文档描述 v0.1 原型基线与 v0.2 账户层（v0.2.0）；与代码行为冲突时，以 README、测试和最新 ADR 为准。

## `.ledger-kernel/` 布局

```text
.ledger-kernel/
├── kernel.toml            # 绑定、write_target、审批策略
├── contracts.yaml         # 声明式账户契约（schema_version 1）
├── templates.yaml         # 草稿模板（schema_version 1）
├── state.json             # 当前 revision/manifest/状态与 reconciliations 缓存（可重建）
├── kernel.lock            # 内核写入串行锁（flock）
├── idempotency.json       # agent + key -> draft_id
├── drafts/<id>.json       # 不可变提案正文
├── drafts/<id>.state.json # 工作流状态、批准记录
├── validations/<id>.json  # 校验结果、fragment、diff、review_digest、manifest
├── audit/events.jsonl     # 追加式哈希链审计
├── journal/<commit>.json  # PREPARED/COMMITTED/ABORTED/RECOVERY_REQUIRED
├── snapshots/blobs/<sha>  # 内容寻址快照（去重）
├── snapshots/<commit>/    # 该次提交的写前副本
└── observations/<id>.json # 外部变化观测；同名 .diff
```

其中 `state.json`、`snapshots/` 可由账本、契约和审计副本重建或丢弃；草稿、validation、审计、journal 是工作流记录，不应删除。

## 账户契约

```yaml
schema_version: 1
unmanaged_accounts:
  - Assets:Savings:Cash
accounts:
  Assets:Bank:Checking:
    aliases: [工资卡, 主银行卡]
    policy:
      allowed_agents: [receipt-agent]
      allowed_currencies: [CNY]
      allowed_tags: [reimbursement]
      required_tags: []
      reconciliation:
        cadence: monthly
        hard_deadline: false
      balance_policy:
        mode: warn_only
        min: null
        max: null
```

硬阻断：账户未开立/已关闭、`allowed_agents`、`allowed_currencies`、`allowed_tags`、`required_tags`。
仅警告：`reconciliation.cadence`、`balance_policy`、未知契约账户、低置信度、证据缺失/哈希不符。

`unmanaged_accounts` 显式声明“知道该账户存在，但暂不纳入契约管理”：不能与 `accounts` 重叠，账户名仍须是合法 Beancount 账户。契约覆盖率 = 有契约的活跃账户 / 活跃账户；`unmanaged_accounts` 单独计数：

```bash
ledger-kernel accounts list --coverage
```

契约文件未知字段、无效账户名、重复别名会直接 `CONTRACT_INVALID` 并阻断受控提交（fail-closed）；读取仍可用。v0.1 不支持任意 Python 规则。

### 契约编辑（v0.2）

契约文件的唯一写入入口是 `contracts apply`，不再“手改 YAML 后祈祷”：

```bash
ledger-kernel contracts show                                  # 解析结果、revision、覆盖率、错误
ledger-kernel contracts validate --file contracts.new.yaml    # 严格解析；不写文件
ledger-kernel contracts diff --file contracts.new.yaml        # 统一 diff 与受影响账户
ledger-kernel contracts apply --file contracts.new.yaml \
  --expected-current sha256:<current> --confirm sha256:<new> --note "加电费契约"
```

- `--expected-current`（调用者读到的当前 revision；文件不存在时写 `absent`）与 `--confirm`（新文件完整 SHA-256）必须同时提供且匹配；缺失或不符返回 `CONTRACT_CONFLICT`，文件不变。
- 写入前严格校验：未知字段、重复别名、非法账户名、cadence/余额区间、`unmanaged_accounts` 重叠均返回 `CONTRACT_INVALID`，不写文件。
- 写入使用同目录临时文件 + `fsync` + `os.replace` + 目录 `fsync`，持有 `kernel.lock`；旧/新内容进入 `snapshots/blobs/`，并追加 `ContractChanged` 审计（前后 hash、actor、note）。
- 契约 revision 变化使所有 pending validation/approval 失效，下一次操作自动重验；不沿用旧批准。
- 契约编辑不写 Ledger、不改变 `ledger_revision`；MCP 不暴露该写入口。

### 账户对账（v0.2）

对账是**工作流事实**，不是 Beancount 指令：`accounts reconcile` 追加 `AccountReconciled` 审计事件，`accounts reconciliation revert` 追加 `AccountReconciliationReverted`；两者都不写 Ledger、不改变 `ledger_revision`，审计历史永不删除或改写。

```bash
ledger-kernel accounts reconcile Assets:Bank:Checking --as-of 2026-11-30 \
  --declared CNY=1234.56 --declared USD=100.00 --note "与银行 App 一致" \
  --confirm Assets:Bank:Checking
ledger-kernel accounts reconciliation list Assets:Bank:Checking
ledger-kernel accounts reconciliation revert rec_... --reason "余额抄错" --confirm rec_...
```

- 记录字段：`reconciliation_id`、`account`、`as_of`、`ledger_balances`（该账户 `as_of` 日终余额，多币种）、`declared_balances`（可选申报余额）、`differences = declared - ledger`（按申报币种逐项）、`declared_not_in_ledger`（申报币种不在账本余额时按 ledger=0 计算并在此列出）、`ledger_revision`、`contract_revision`、`actor`（`human/local-user`）、`recorded_at`、`note`（可选）。金额一律使用十进制字符串；`float` 或非法金额返回 `RECONCILIATION_INVALID`。
- 对账状态由契约 `policy.reconciliation.cadence`（`weekly/monthly/quarterly/yearly`）与最近有效记录推导：`not_configured` / `never` / `current` / `overdue`；`next_due = last.as_of + cadence_days`；`difference_status` 为 `none/zero/nonzero`。`accounts show` 同时返回 `assertion_status`（`unset`/`asserted`）、`last_assertion`（最近一条 Beancount `balance` 指令的日期与金额；没有则为 `null`）与 `reconciliation` 视图；断言失败属于全局 `LedgerInvalid`，不在账户视图伪装通过。`accounts list` 的每一行也包含同样三个字段，便于一次查看全部账户的对账/断言状态。
- 同一账户同一 `as_of` 只允许一条有效记录（先撤销再重记）；只能撤销该账户最新的有效记录，撤销后状态回退到上一条记录，历史记录仍保留在审计链中。
- 前置条件：账本加载无错误、账户在 `as_of` 已开立未关闭、`as_of <= today`；否则返回 `RECONCILIATION_INVALID` 且不写入任何记录。对账不要求证据，`note` 可选。
- **对账永不阻断**：状态只用于提示、排序与审核视图，`CONTRACT_RECONCILIATION` 仅在 `never`/`overdue` 时警告；`hard_deadline` 兼容读取但被忽略。申报差异只记录、只展示，不自动生成调整分录、不自动提交；需要调整时仍走草稿 → validate → approve → commit。
- `state.json.reconciliations` 是可重建缓存；缺失或损坏时从审计事件重建，读取以审计链为准。
- `accounts reconcile` / `accounts reconciliation revert` 要求 `--confirm` 等于账户名 / `reconciliation_id`，非交互模式不会自动确认；MCP 不暴露对账写入口。

### 按账户审核（v0.2）

账户级审核是**只读分组视图**，不引入账户级批准对象（ADR-020）：

```bash
ledger-kernel review --by-account                   # 按账户聚合待审草稿
ledger-kernel review --account Assets:Bank:Checking # 只看该账户；即使没有待审草稿也显示该组
ledger-kernel review --by-account --state Approved   # --state 过滤仍先生效
```

- 分组规则：默认只出现“有 pending 草稿（沿用 `review` 队列的状态集合）的账户”；一条草稿会出现在它 `account_refs` 里的每个账户分组下。`--account` 即使该账户没有待审草稿也显示一个组。
- 每组字段（`--json` 输出的 `groups[]`）：`account`、`lifecycle`（生命周期）、`balances`（余额）、`reconciliation`（`status` / `next_due` / `difference_status`，含最近一条有效记录）、`contract`（有契约时为 `aliases` / `allowed_agents` / `allowed_currencies` / `allowed_tags` / `required_tags` / `reconciliation` / `balance_policy`，无契约时为 `null`）、`pending_drafts`（`draft_id` / `date` / `amount` / `currency` / `narration` / `state` / `validation_status`）、`warnings`（草稿 latest validation 中的 `CONTRACT_*` warning，按检查的 `account` 归入对应账户组，没有 `account` 的检查归入草稿出现的组；只含 `severity: "warning"`，不会复制成阻断）、`draft_count`。
- 顶层 JSON：`mode: "by_account"`、`account`（过滤账户或 `null`）、`note`（通常为 `null`；未知账户时说明原因）、`groups`。文本输出每组一行 `== 账户 生命周期 余额 reconciliation 状态（next_due）` 头部，随后是 contract 摘要行、待审行与 warning 行；空组显示 `pending: none`，无契约组显示 `contract: none`。
- 未知账户（账本、契约与所有草稿中都未出现）：返回 `groups: []` 且 `note` 为 `unknown account: <name>`，不返回错误码；文本输出该 note。
- **批准绑定不变**：`review --by-account --approve` 在没有显式 draft_id 列表时仍返回 `INVALID_STATE`；有显式 draft_id 与 `--confirm` 时走与 v0.1 相同的逐 draft 批准路径，仍绑定 `draft_id + validation_id + ledger_revision + contract_revision`，新增审计事件仍只有逐 draft 的 `DraftApproved`。不提供“按账户一键批准”；不带 `--approve` 时分组视图与显式 draft_id 参数互斥（同时给出返回 `INVALID_STATE`），带 `--approve` 时仍按显式 draft_id 逐笔批准、分组开关不改变批准语义。现有 `review` 队列/逐笔详情与 `drafts approve` 行为不变。
- 分组视图只读：不写 Ledger、不改变 `ledger_revision`、不新增审计事件或状态机状态；MCP 工具列表不变（不新增按账户审核工具）。

## 已冻结的工程决策（对应实施计划第 12 节）

| 事项 | v0.1 选择 |
|---|---|
| Beancount 版本 | 3.2.x（本机验证 3.2.3），依赖 `>=3.2,<4`；升级须跑真实账本回归并更新 engine fingerprint。fingerprint 只含 Beancount 与 ledger-kernel 版本，不含 Python 运行时（ADR-014） |
| include 闭包 | `loader.load_file()` 的 `options_map["include"]`，并用根文件、entry source、文件系统三重交叉校验；缺失时退化为 entry meta + root 并记录 `closure_source`；拒绝账本目录之外的 include |
| 外部快照 | 闭包内所有文件的内容寻址 blob；默认上限 512 MiB；当前 manifest、journal、before/after hash 受保护 |
| 提交原语 | 同目录临时文件 + `fsync` + `os.replace` + 目录 `fsync`；最终 revision 复查；不使用 `O_APPEND`（两套原语不并存） |
| 历史错误基线 | 要求干净基线；当前账本有 Beancount 错误时阻断 validate/commit |
| 契约存储 | `.ledger-kernel/contracts.yaml`，`schema_version: 1`，严格字段，声明式 |
| MCP 传输 | stdio，一行一条 JSON-RPC；只暴露读取与提案 |

### v0.2 账户层冻结决策（对应 v0.2 实施计划第 0 节）

| 事项 | 选择 |
|---|---|
| 契约编辑 | 唯一写入口 `contracts apply`：`--expected-current` + `--confirm` 双哈希 CAS、原子替换、`ContractChanged` 审计与前后内容 blob；失败不改文件（ADR-019） |
| 账户对账 | 审计事件 `AccountReconciled` / `AccountReconciliationReverted` 追加式记录；状态按 `cadence` 与日期推导，不要求证据、永不阻断写入；`hard_deadline` 兼容读取但忽略（ADR-018） |
| 申报余额 | 可选 `--declared CUR=VALUE`；记录 `ledger_balances / declared_balances / differences`，差异只读，不自动调整、不自动提交 |
| 账户级审核 | `review --by-account/--account` 只读分组视图；不新增账户级批准对象，批准仍绑定 `draft_id + validation_id + ledger_revision + contract_revision`（ADR-020） |

## 验收测试矩阵

运行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

| 编号 | 场景 | 测试 |
|---|---|---|
| T01 | 加载普通账本 | `test_load_plain_ledger` |
| T02 | 多 include 纳入 revision | `test_include_files_are_in_revision` |
| T03 | 修改非 write_target include | `test_non_target_include_change_changes_revision`、`test_external_change_invalidates_validation` |
| T04 | 内核关闭时手改账本 | `test_external_edit_while_kernel_closed_is_observed_not_overwritten` |
| T05 | Agent 提交有效草稿 | `test_submit_does_not_touch_ledger` |
| T06 | 幂等重复提交 | `test_idempotency_returns_same_draft` |
| T07 | 违反 Beancount 语义 | `test_unbalanced_transaction_is_blocked_by_beancount` |
| T08 | 违反硬账户契约 | `test_hard_contract_is_blocked` |
| T09 | 批准前外部变化 | `test_external_change_invalidates_validation` |
| T10 | 批准后写入前外部变化 | `test_external_change_after_approval_blocks_commit` |
| T11 | 两个内核同时写入 | `test_second_kernel_cannot_hold_lock` |
| T12 | 提交中崩溃 | `test_crash_after_replace_recovers_without_duplicate` 等 |
| T13 | 来源未知 | `test_external_edit_while_kernel_closed_is_observed_not_overwritten` |
| T14 | 外部修改导致无效 | `test_invalid_external_edit_blocks_controlled_commits` |
| T15 | 撤销已有外部交易 | `test_reversal_is_a_new_draft_not_a_delete`（生成冲销草稿，不删除历史） |
| T16 | 删除派生状态 | `test_deleting_derived_state_rebuilds_from_ledger` |
| T17 | narration 注入 | `test_newline_injection_is_blocked` |
| T18 | payee 引号/反斜杠 | `test_quotes_and_backslashes_survive_commit` |
| T19 | 渲染器缺陷多一条 | `test_renderer_defect_adding_an_entry_is_delta_mismatch` |
| T20 | 篡改待写入片段 | `test_tampered_fragment_is_rejected_at_commit` |
| T21 | 批量批准沿用 | `test_batch_approvals_carry_forward` |
| T22 | digest 变化断沿用 | `test_digest_change_breaks_carry_forward` |
| T23 | 批中途外部变化 | `test_external_change_mid_batch_invalidates_remaining_approvals` |
| T24 | 提交后外部追加 | `test_external_append_after_commit_is_not_a_known_transition` |

### v0.2 账户层验收

| 编号 | 场景 | 测试 |
|---|---|---|
| A01 | 契约 apply：替换文件、`ContractChanged` 审计、前后 blob、`ledger_revision` 不变 | `test_apply_replaces_file_records_audit_and_keeps_ledger_revision` |
| A02 | 契约 CAS 冲突/确认不符/缺失哈希 → `CONTRACT_CONFLICT`，文件不变 | `test_apply_rejects_stale_expected_current_without_writing`、`test_apply_requires_expected_current`、`test_apply_requires_matching_confirm_hash` |
| A03 | 非法 schema、`unmanaged_accounts` 重叠 → `CONTRACT_INVALID`，文件不变 | `test_apply_rejects_invalid_contracts_without_writing` |
| A04 | 契约变更使 pending validation/approval 失效，重验后可批准 | `test_apply_invalidates_pending_validation_and_approval` |
| A05 | 契约覆盖率与 `unmanaged_accounts` 计数 | `test_contracts_view_warns_about_unopened_accounts`、`test_accounts_list_coverage_reports_managed_and_unmanaged` |
| A06 | 对账记录成功、审计事件、`ledger_revision` 不变 | `test_record_success_persists_record_and_audit_event` |
| A07 | 对账状态 `current/overdue/never/not_configured` 与 `next_due` | `test_current_status_and_next_due`、`test_overdue_status_and_next_due`、`test_never_and_not_configured_statuses` |
| A08 | 申报差异：未申报/零/非零/多币种/不在账本余额 | `test_zero_difference_is_recorded`、`test_nonzero_difference_is_recorded`、`test_multi_currency_balances_and_differences`、`test_declared_currency_absent_from_ledger_uses_zero` |
| A09 | 无效账本/账户未开立/`as_of` 未来/重复 `as_of` → `RECONCILIATION_INVALID`，不写 | `test_invalid_ledger_is_rejected`、`test_account_not_open_on_as_of_is_rejected`、`test_future_as_of_is_rejected`、`test_duplicate_as_of_is_rejected` |
| A10 | 撤销只能最新、状态回退，历史保留 | `test_revert_requires_the_latest_record`、`test_revert_restores_previous_status_and_keeps_history` |
| A11 | 缓存缺失/损坏时从审计重建，读取以审计为准 | `test_missing_state_json_rebuilds_cache_from_audit`、`test_corrupt_state_cache_rebuilds_from_audit`、`test_reads_derive_truth_from_audit_not_from_cache` |
| A12 | 对账逾期仍不阻断提交 | `test_overdue_warning_still_does_not_block_commit` |
| A13 | `review --by-account` 分组与 `--account` 过滤 | `test_by_account_groups_each_draft_under_its_accounts`、`test_account_filter_limits_to_one_group` |
| A14 | 分组视图不改变批准绑定 | `test_by_account_approve_requires_explicit_ids_and_binds_each_draft` |

端到端检查分两层，都只操作临时副本：

合成 fixture（默认，CI 每次构建执行；覆盖提交、外部修改、草稿失效、无效账本阻断）：

```bash
PYTHONPATH=src python3 scripts/e2e_ledger.py
```

真实账本副本（涉及写入/恢复/外部变化时本地执行，`--source` 原目录只读）：

```bash
PYTHONPATH=src python3 scripts/e2e_ledger.py --source /path/to/TestAssets
```

## 已知限制（如实声明）

- **平台**：当前锁与目录 `fsync` 实现面向 POSIX（`fcntl.flock`）；Windows 需替换锁实现后再用。
- **并发**：`flock` 只协调内核写入。普通编辑器不遵守锁；最后 hash 检查与 `os.replace` 之间仍存在理论竞态窗口。v0.1 降低窗口、检测前后漂移，但不承诺对不配合的进程实现通用 compare-and-swap。
- **归因**：只证明内容变化；不证明操作者。`source_kind` 固定为 `external_unknown`、`attribution_confidence=unknown`，不虚构 Agent/用户身份；尚未实现 Git/OS audit 佐证接入。
- **插件**：账本插件是用户信任的代码执行环境。Dry-run 会在临时目录执行同一插件配置；依赖绝对路径或外部副作用的插件可能与真实加载不完全等价。Agent 不能指定插件。
- **附件**：Dry-run 用符号链接镜像非闭包顶层目录（如 `document/`），以兼容 Beancount `Document` 指令；这些是真实文件的只读视图，不会被复制或写回。
- **余额**：diff 中的余额是 posting 原始数量按币种求和，不做成本/价格换算；Beancount 的完整 balance assertion 仍由官方加载器校验。
- **多文件事务**：一次提交只写一个 `write_target`；不支持批量重写、删除历史、跨文件事务。
- **修正/冲销**：`corrections propose <commit_id>` 只生成冲销草稿，仍须 validate/approve/commit；不删除、不改写原交易。外部直接写入的交易没有稳定的 `commit_id`，暂不能一键生成冲销草稿。
- **MCP**：stdio 最小实现（initialize / tools/list / tools/call）；未做鉴权、流式进度、资源订阅。远程接入必须自行加安全通道。
- **快照保留**：默认 512 MiB 上限，超出后按最旧未受保护 blob 清理；不会删除当前 manifest/journal 引用的基线。
- **账户层元数据**：契约编辑的双哈希 CAS 只防内核并发与误写；普通编辑器仍可在哈希检查与 `os.replace` 之间制造理论竞态。对账状态是日期驱动的工作流提示，不因后续提交自动失效；申报差异只读，不生成调整分录。
- **按账户审核**：`review --by-account` 是只读分组视图，不支持按账户批量批准；账户级快捷操作留待 v0.3 评估（ADR-020）。
- **性能**：读与元数据操作在闭包字节未变时复用已加载结果；首次加载与 `validate` 的完整 dry-run 仍是主要成本（134 文件真实账本副本首次约 0.7 s、`validate` 约 1.1 s/笔）。审计链 append 仍为 O(n)，事件数很大时需索引（见 [dogfood 轮次报告](docs/design/v0.2-dogfood-round2.md)）。

## 代码结构

```text
src/ledger_kernel/
├── cli.py            # 人工审核、契约编辑与对账 CLI
├── mcp_server.py     # MCP stdio 适配器（只读 + 提案）
├── kernel.py         # 用例编排：状态、草稿、批准、提交、恢复、契约与对账
├── adapter.py        # Beancount 加载/闭包/账户/余额/规范 entry
├── dryrun.py         # 临时工作区、完整校验、ADR-012 增量断言、diff
├── schema.py         # 草稿 schema、文本安全、Beancount 渲染
├── contracts.py      # 声明式账户契约、解析/校验与覆盖率
├── revision.py       # manifest + ledger/contract revision
├── store.py          # 草稿/validation/审计/ journal/观测/ blob
├── config.py         # kernel.toml
├── locks.py          # flock
├── errors.py         # 稳定错误码
└── util.py           # 原子写、hash、文本、金额
```

## 开发

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
PYTHONPATH=src python3 scripts/e2e_ledger.py
PYTHONPATH=src python3 scripts/e2e_ledger.py --source /path/to/TestAssets
```

测试不依赖 pytest；只依赖 `beancount` 与 `PyYAML`。测试与 E2E 全部在临时目录中运行，不会修改真实账本；合成 fixture 位于 `tests/fixtures/ledger`。
