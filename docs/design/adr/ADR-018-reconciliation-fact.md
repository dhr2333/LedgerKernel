# ADR-018：对账是工作流事实，由审计事件记录并按日期推导状态

状态：[提议]

**背景**
v0.1 只有契约里的 `reconciliation.cadence`，在写入时用“最后记账日超过 cadence”给一条警告；没有“用户确认过某账户某日余额”的记录，也无法回答“这个账户上次对账是什么时候、是否逾期”。账户语义第二层（状态）缺少对账事实。

**决策**
1. 对账记录是**内核工作流事实**，不是 Beancount 指令：`AccountReconciled` 追加到哈希链审计，包含 `reconciliation_id / account / as_of / balances / ledger_revision / contract_revision / actor / recorded_at / note`。
2. 对账不改变 `ledger_revision`，只追加审计事件；`state.json.reconciliations` 是可重建缓存，不是真相。
3. 对账状态由“最近有效记录 + cadence + 今天”推导：`not_configured / never / current / overdue`；`hard_deadline` 在 v0.2 仍只提示，不阻断写入。
4. 撤销对账追加 `AccountReconciliationReverted`；只能撤销该账户最新的有效记录，历史不删除。
5. 对账与撤销都必须由本地人类执行并显式确认（`--confirm` 等于账户名/记录 ID）；MCP 不暴露写入口。
6. 对账前要求账本加载无错误、账户在 `as_of` 已开立/未关闭、`as_of <= today`；否则 `RECONCILIATION_INVALID`，不写任何东西。

**后果**
- 账户状态从“只能看余额”变为“可回答是否对账、下次何时到期”。
- 审计链继续作为唯一追加式记录；缓存丢失可从审计重建。
- v0.2 对账不阻断交易，避免把未确认的语义变成硬约束。

**验收**
- 记录、状态推导、过期、撤销、重复 as_of、无效账本等用例（见 `v0.2-account-layer.md` 第 9 节）。
- 对账前后 `ledger_revision` 不变。
