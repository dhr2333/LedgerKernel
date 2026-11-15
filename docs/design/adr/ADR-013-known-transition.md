# ADR-013：区分"已知转移"与"外部变化"；批准可在严格条件下沿用

状态：[建议冻结]

**背景**
`ledger_revision` 覆盖整个 include 闭包（含 `write_target`），内核每次提交都会改变它。若所有绑定旧 revision 的 validation/approval 都因此失效，一次审 30 笔就要重新确认 30 轮，闭环会比手工记账更慢。

**决策**
1. **已知转移（Known Transition）判据**：当前 revision 严格等于"上一次已观测的 manifest 中，仅把 `write_target` 的哈希替换为 journal 记录的 `after_hash`"所得的 revision，且该 journal 条目为 COMMITTED。
   - 满足：记为 `KernelTransition(commit_id)`，不产生 `ExternalChangeObserved`，不触发 stale。
   - 不满足（`write_target` 之外任何文件变化、`write_target` 哈希与 `after_hash` 不一致、journal 缺失）：一律按外部变化处理，现有 stale 逻辑不变。
2. 契约 revision 与 `engine_fingerprint` 的变化永远不是已知转移，始终使全部 validation/approval 失效。
3. 草稿的 `base_revision` 降为信息字段，只记录提案方看到的版本，用于审计排查，不参与有效性判定。有效性由 validation 绑定的 revision 决定。
4. 已知转移之后，所有处于 NeedsHumanApproval 的草稿自动重跑 dry-run，生成绑定新 revision 的 validation。用户批准的始终是当前 revision 上的 validation，不再被"已过期"打断。
5. **批准沿用**（仅限同一次批量批准产生的批次，不跨会话；approval 带 `expires_at`）：每笔提交前重新计算 revision 并重跑完整 dry-run。若 revision 差异完全由已知转移链解释，且新旧 `review_digest` 相等，沿用该批准；否则批准作废，草稿回到 NeedsHumanApproval。沿用不是跳过校验。
   `review_digest = hash(fragment_sha256, 受影响账户集合, 阻断/警告集合(code, account, severity))`，不含余额数值。
6. 取舍：digest 不含余额数值，因此用户批准时看到的"余额 1000→880"，沿用时可能变成"880→760"。交易内容、受影响账户和告警集合不变，视为同一次批准。配置项 `approval_carry_forward` 可关闭；关闭后退化为只有第 4 条的收益。
7. 审计新增事件 `ApprovalCarriedForward {approval_ref, from_validation, to_validation, review_digest, via_commit_ids}`。

**后果**
- 状态机增加 `Clean → KernelTransition → Clean` 分支。
- 规格 4.1 中 `Stale` 的触发条件收窄为：外部变化、契约变化、engine 变化。
- 需要批量批准入口（如 `approve_drafts(batch)`），仍只对用户前端/CLI 开放。

**验收**
- T21：批量批准 30 笔，一次完成，无需二次确认，审计中有 29 条 `ApprovalCarriedForward`。
- T22：第 k 笔因前面的提交导致余额断言失败 → digest 变化，该笔回到 NeedsHumanApproval，其余各笔单独判定。
- T23：提交中途用户编辑另一个 include 文件 → 其余批准全部作废。
- T24：提交后 `write_target` 被外部追加一行（hash ≠ `after_hash`）→ 不是已知转移，按外部变化处理。
