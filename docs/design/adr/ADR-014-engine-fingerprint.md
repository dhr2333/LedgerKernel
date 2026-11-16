# ADR-014：engine fingerprint 不绑定 Python 运行时

状态：[建议冻结]

**背景**
`engine_fingerprint` 原先由「Beancount 版本 + Python 完整版本 + ledger-kernel 版本」组成，并被写入 `ledger_revision`、validation 与 approval 的绑定。dogfood 中出现了一次真实故障：起草端用 Python 3.14.7，审核端用 Python 3.12.9，同一账本触发 `EngineChanged`，9 篇草稿全部 `DraftInvalidated`，批准在写入前被拒绝且只在审计链留痕。审核端与起草端使用不同解释器是正常情况，不应让批准失效。

**决策**
1. `engine_fingerprint()` 只包含 **Beancount 版本** 与 **ledger-kernel 版本**，不再包含 Python 版本。
2. Python 运行时属于执行环境，不属于账本语义；同一 Beancount 版本在不同 Python 小版本上的行为差异，由 Beancount 版本与回归测试覆盖，而不是由批准绑定覆盖。
3. `EngineChanged` 仍会触发，但只在 Beancount 或 ledger-kernel 版本变化时触发；审计事件保留 `previous_fingerprint` 与 `current_fingerprint`。
4. 升级后账本的 `ledger_revision` 会变化一次，旧 validation/approval 按既有规则失效；这是预期的一次性代价。

**后果**
- 审核端可以在不同 Python 解释器上查看与批准同一批草稿。
- 升级 Beancount 或 ledger-kernel 仍然会使全部 validation/approval 失效，fail-closed 不变。
- 需要更新 fingerprint 相关测试与 README 的冻结决策表。

**验收**
- `engine_fingerprint()` 不含 `python` 子串（`tests/test_adapter.py::test_engine_fingerprint_does_not_bind_python_runtime`）。
- 在 Python 3.12 与 3.14 下对同一账本执行 `status`，fingerprint 相同。
- 升级 Beancount 版本仍触发 `EngineChanged` 并使草稿失效（既有测试覆盖 engine 失效路径）。
