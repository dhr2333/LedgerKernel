# ADR-015：review digest 只覆盖被审核的交易内容，不含追加分隔符

状态：[建议冻结]

**背景**
ADR-013 第 5 条把 `review_digest` 定义为 `hash(fragment_sha256, 受影响账户集合, 阻断/警告集合)`。`fragment_sha256` 覆盖的“待追加字节”包含前导空行分隔符，而分隔符取决于 `write_target` 当前内容：

- 文件为空时，第一笔的 fragment 没有前导换行；
- 第一笔提交后文件非空，后续草稿的 fragment 多出前导换行。

dogfood 中这导致一次真实故障：9 篇草稿批量批准后，首笔提交成功，其余 8 篇因 `review_digest` 变化全部回到 `NeedsHumanApproval`，批量批准只兑现 1 笔（`APPROVAL_REQUIRED`）。人类审核的是交易内容，不是分隔符。

**决策**
1. `review_digest` 改为 `hash(rendered_sha256, 受影响账户集合, 阻断/警告集合)`。
2. `rendered_sha256` 是 `render_fragment(transactions)` 的哈希，即交易正文，不含为拼接目标文件而添加的前导分隔符。
3. `fragment_sha256` 不变，仍覆盖提交时实际追加的全部字节，继续用于 `FRAGMENT_MISMATCH` 与提交前片段校验。
4. 本 ADR 取代 ADR-013 第 5 条中 `review_digest` 的组成定义；ADR-013 其他规则（已知转移判据、沿用条件、过期时间）不变。

**后果**
- 空 `write_target` 的首次批量入账不再需要二次批准；batch commit 可一次完成。
- 人类批准后，若目标文件仅因前导分隔符变化而不同，批准继续有效；交易正文、账户或告警变化仍使 digest 变化并使批准失效。
- 旧 validation 里的 digest 采用旧算法，升级后首次批量会按既有规则重新验证/批准一次。

**验收**
- 空 `write_target` 下批准两笔、提交第一笔后，第二笔仍为 `Approved` 且 `review_digest` 不变，最终两笔都 `Committed`（`tests/test_commit.py::test_batch_approvals_carry_forward_on_empty_write_target`）。
- 提交时篡改 fragment 仍触发 `FRAGMENT_MISMATCH`（既有测试）。
