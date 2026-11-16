# ADR-016：草稿模板只做字段预填，不绕过审核

状态：[建议冻结]

**背景**
dogfood 中同类交易（电费、水务、外卖、医院充值）反复填写相同的商户、账户、标签与付款方式。Agent 每次重写整份 payload，既慢又容易写错账户；人工审核端也无法复用已确认过的映射。

**决策**
1. 新增 `.ledger-kernel/templates.yaml`（`schema_version: 1`），声明式保存模板；`init` 创建空文件。
2. 模板只包含可复用字段：`payee`、`narration`、`tags`、`currency`、`postings`。posting 的 `units` 支持 `amount`、`-amount` 占位符或固定十进制字符串。
3. `ledger-kernel templates list|show` 只读展示；`ledger-kernel drafts submit --template NAME --amount ... --date ...` 把模板展开为普通 draft payload。
4. 模板展开后仍走 `submit_draft → validate → approve → commit`，不新增写入路径，不改变批准绑定，不提供自动批准。
5. 模板文件格式错误、未知字段、金额占位符缺失、未知模板名一律 `TEMPLATE_INVALID`（fail-closed）。
6. `drafts submit` 同时支持 `--payload <file>`，作为 Agent/脚本的统一提交入口；幂等键缺省为 payload 内容哈希。

**后果**
- 同类交易只需提供金额与日期，商户/账户/标签由模板固定；审核端仍看到完整草稿与 dry-run。
- 模板文件与账本一起版本化；模板本身不是事实源，账本仍是唯一真相。
- 新增 CLI 面与公开错误码 `TEMPLATE_INVALID`，需要中英文 README 与 CHANGELOG。

**验收**
- 模板展开后的 payee/tags/postings 与模板一致（`tests/test_cli_mcp.py::test_draft_template_expands_repeated_fields`）。
- 未知模板与缺金额返回 `TEMPLATE_INVALID`，不产生草稿。
- 模板草稿仍需人工 approve 才能 commit（既有状态机测试覆盖）。
