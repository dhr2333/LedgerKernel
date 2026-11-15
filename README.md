# LedgerKernel v0.1

本地账本账户内核：围绕 Beancount 账本提供**提案 → 完整 Dry-run → 人工确认 → 受控追加 → 审计 → 恢复**的闭环。

账本文件是财务事实的唯一真相源；内核是所有**自动化写入**的唯一受控入口。用户保留直接编辑账本的最高权限，内核不阻止、不覆盖，只负责发现、记录，并在无法安全归因时明确标记 `external_unknown`。

> 本仓库是 v0.1 原型实现，对应设计库中的《顶层设计摘要》《本地账户内核_v0.1_工程决策冻结与原型实施计划》《本地账户内核_v0.1_设计规格草案》与 ADR-012/ADR-013。实现覆盖 P0-A～P0-D 及薄 MCP 适配器（P0-E 的最小工具集）。

## 不是什么

- 不是记账 App、预算工具、消费分析仪表盘。
- 不内置 AI 推理，不绑定模型；Agent 在进程外，只提交结构化草稿。
- 不承诺“Agent 永远无法直写账本”。同一 OS 用户下无法可靠区分 Agent 与用户；直写按外部变化处理。
- 不把 Git 历史当审计，不把 hash 当身份认证。
- v0.1 不做自动审核分级：所有草稿必须人工批准。

## 快速开始

要求 Python ≥ 3.11、Beancount 3.2.x。

```bash
cd /home/dhr2333/GitHub/LedgerKernel
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

**不暴露**批准、提交、写文件、修改契约的工具。MCP 层不重新实现校验或写入逻辑，只调用 `Kernel` 用例。

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
8. **外部变化**：每次操作前重算闭包指纹；发现变化时重新加载、保存 diff（有旧 blob 时）、失效旧草稿、记录 `ExternalChangeObserved`。外部变化导致账本无效时进入 `LedgerInvalid`，阻断受控提交，但不改用户文件。
9. **已知转移**：只有“当前 revision = 旧 manifest 仅替换 `write_target` 为某条 COMMITTED journal 的 after_hash”才视为内核自身提交；其他一律按外部变化。批量批准可在 `review_digest` 不变时沿用批准（`ApprovalCarriedForward`），不跳过重验。
10. **审计链**：`events.jsonl` 每条事件带 `prev_hash` 与 `event_hash`；链损坏/为空时拒绝提交（`AUDIT_INTEGRITY`）。

## `.ledger-kernel/` 布局

```text
.ledger-kernel/
├── kernel.toml            # 绑定、write_target、审批策略
├── contracts.yaml         # 声明式账户契约（schema_version 1）
├── state.json             # 当前 revision/manifest/状态（可重建）
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

契约文件未知字段、无效账户名、重复别名会直接 `CONTRACT_INVALID` 并阻断受控提交（fail-closed）；读取仍可用。v0.1 不支持任意 Python 规则。

## 已冻结的工程决策（对应实施计划第 12 节）

| 事项 | v0.1 选择 |
|---|---|
| Beancount 版本 | 3.2.x（本机验证 3.2.3），依赖 `>=3.2,<4`；升级须跑真实账本回归并更新 engine fingerprint |
| include 闭包 | `loader.load_file()` 的 `options_map["include"]`，并用根文件、entry source、文件系统三重交叉校验；缺失时退化为 entry meta + root 并记录 `closure_source`；拒绝账本目录之外的 include |
| 外部快照 | 闭包内所有文件的内容寻址 blob；默认上限 512 MiB；当前 manifest、journal、before/after hash 受保护 |
| 提交原语 | 同目录临时文件 + `fsync` + `os.replace` + 目录 `fsync`；最终 revision 复查；不使用 `O_APPEND`（两套原语不并存） |
| 历史错误基线 | 要求干净基线；当前账本有 Beancount 错误时阻断 validate/commit |
| 契约存储 | `.ledger-kernel/contracts.yaml`，`schema_version: 1`，严格字段，声明式 |
| MCP 传输 | stdio，一行一条 JSON-RPC；只暴露读取与提案 |

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

真实账本端到端（在 TestAssets 脱敏副本上执行，不改原目录）：

```bash
PYTHONPATH=src python3 scripts/e2e_testassets.py
```

## 已知限制（v0.1 如实声明）

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

## 代码结构

```text
src/ledger_kernel/
├── cli.py            # 人工审核 CLI
├── mcp_server.py     # MCP stdio 适配器（只读 + 提案）
├── kernel.py         # 用例编排：状态、草稿、批准、提交、恢复
├── adapter.py        # Beancount 加载/闭包/账户/余额/规范 entry
├── dryrun.py         # 临时工作区、完整校验、ADR-012 增量断言、diff
├── schema.py         # 草稿 schema、文本安全、Beancount 渲染
├── contracts.py      # 声明式账户契约
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
PYTHONPATH=src python3 scripts/e2e_testassets.py
```

测试不依赖 pytest；只依赖 `beancount` 与 `PyYAML`。测试全部在临时目录中运行，不会修改真实账本。
