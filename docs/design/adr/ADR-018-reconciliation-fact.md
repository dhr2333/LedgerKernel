# ADR-018：对账是工作流事实，由审计事件记录并按日期推导状态

状态：[建议冻结]

**背景**
v0.1 只有契约里的 `reconciliation.cadence`，在写入时用“最后记账日超过 cadence”给一条警告；没有“用户确认过某账户某日余额”的记录，也无法回答“这个账户上次对账是什么时候、是否逾期”。账户语义第二层（状态）缺少对账事实。

**决策**
1. 对账记录是**内核工作流事实**，不是 Beancount 指令：`AccountReconciled` 追加到哈希链审计，包含 `reconciliation_id / account / as_of / balances / ledger_revision / contract_revision / actor / recorded_at / note`。
2. 对账不改变 `ledger_revision`，只追加审计事件；`state.json.reconciliations` 是可重建缓存，不是真相。
3. 对账状态由“最近有效记录 + cadence + 今天”推导：`not_configured / never / current / overdue`；状态只用于提示、排序与审核视图，**永不阻断**写入。`hard_deadline` 字段视为已弃用：兼容读取、忽略其值。
4. 撤销对账追加 `AccountReconciliationReverted`；只能撤销该账户最新的有效记录，历史不删除。
5. 对账与撤销都必须由本地人类执行并显式确认（`--confirm` 等于账户名/记录 ID）；MCP 不暴露写入口。
6. 对账前要求账本加载无错误、账户在 `as_of` 已开立/未关闭、`as_of <= today`；否则 `RECONCILIATION_INVALID`，不写任何东西。
7. v0.2 **不要求对账证据**（账单文件哈希/截图）；`note` 可选。
8. 对账可记录可选的 `declared_balances` 与 `differences`（`declared - ledger`），只读展示，不自动生成调整分录、不自动提交；申报币种不在账本余额时按 ledger=0 计算并标记。

**后果**
- 账户状态从“只能看余额”变为“可回答是否对账、下次何时到期、申报余额差多少”。
- 审计链继续作为唯一追加式记录；缓存丢失可从审计重建。
- 对账不阻断交易，避免把未确认的语义变成硬约束；申报差异只读，调整仍走人工批准。

**验收**
- 记录、状态推导、过期、撤销、重复 as_of、无效账本等用例（见 `v0.2-account-layer.md` 第 9 节）。
- 申报余额：未申报/零差异/非零差异/多币种/非法金额。
- 对账前后 `ledger_revision` 不变。
