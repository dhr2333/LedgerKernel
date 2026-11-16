# ADR-020：账户级审核是分组视图，不改变批准绑定与状态机

状态：[建议冻结]

**背景**
v0.2 的方向是从“逐笔交易审核”转向“按账户组织审核”。但批准绑定是 `draft_id + validation_id + ledger_revision + contract_revision`（ADR-013），若引入“账户级批准对象”，会改变状态机、审计事件和沿用规则，风险和收益不成比例。

**决策**
1. v0.2 只增加**只读分组视图**：`review --by-account` / `review --account <name>` 按账户聚合待审草稿、契约摘要、余额与对账状态。
2. 批准语义不变：每个草稿仍单独 validate/approve/commit，批准绑定四元组不变；批量批准仍要求显式 draft_id 列表与 `--confirm`。
3. 不新增“账户级 approval”记录、审计事件或状态机状态；不改变 `ApprovalCarriedForward` 规则。
4. 账户级快捷批准（一次确认批准某账户下全部草稿）留到 v0.3，届时单独 ADR。

**后果**
- 用户获得账户视角，但内核不承担“账户级批准”的新语义。
- 审核 CLI 可以先验证分组工作流是否真的减少往返，再决定是否升级为账户级操作。

**验收**
- 分组输出正确；批量批准仍逐 draft 绑定；审计事件与 v0.1 一致。
- 新增 `review --by-account` 不改变现有 `review`/`drafts approve` 行为。
