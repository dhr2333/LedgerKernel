# ADR-012：Dry-run 增量必须与提案逐项一致（防文本注入）

状态：[建议冻结]

**背景**
完整加载只证明"加上候选交易后账本仍然合法"，不证明"账本只多了提案里的东西"。payee、narration、tag、link、metadata 值来自收据等不可信输入；渲染成 Beancount 文本时，引号加换行就能注入合法的额外指令（`open`、`balance`、`pad`、`option`、`plugin`、`pushtag`），整个账本照样通过校验。CLI 展示同理：终端转义序列可以伪造 diff。

**决策**
1. 只接受结构化提案，不接受 Agent 提交的原始 Beancount 文本。
2. 写入内容只由内核的 `render_transaction` 从结构化字段生成，不做字符串拼接。字段规则：
   - 文本字段（payee、narration、metadata 值）：拒绝控制字符（含 `\n`、`\r`、NUL、U+2028/2029、ESC）；`"` 与 `\` 由渲染器转义；设长度上限。
   - account：符合 Beancount 账户名语法，且在当前账本已 open。新账户走独立操作，不得夹带在交易里。
   - currency / tag / link：限制为 Beancount 语法允许的字符集。
   - 金额：十进制字符串；v0.1 每个 posting 必须显式给出金额，不接受省略金额由 Beancount 插值。
   - v0.1 提案 schema 只含简单 posting（账户、金额、币种）；成本、价格标注、pad 不在 schema 内。
3. 增量断言：dry-run 对加载前后的 entries 做差集，必须同时满足：
   - 旧 entries 全部仍在（无修改、无消失）；
   - 新增 entries 数量等于提案交易数，且全部为 Transaction；
   - 每笔新增交易与提案逐项相等（date、flag、payee、narration、tags、links、postings）；
   - `options_map` 与插件列表不变。
   比较时排除 source metadata（文件名、行号）。具体使用 Beancount 的哪种 entry 比较/哈希能力，由 P0-A 验证。
4. 片段指纹：validation 记录 `fragment_sha256`（渲染出的待追加字节）。提交时重新渲染，hash 与 validation 记录不一致则拒绝；实际写入的字节只能是该片段。
5. 展示隔离：审核界面中的 diff 和余额变化来自内核算出的增量。Agent 自述（`rationale_summary`、证据摘要、payee/narration 原文）放入单独的"不可信文本"区块，转义显示，控制字符可视化，不渲染 markdown/HTML/ANSI，不与 diff 混排。

新增阻断级错误码：`UNSAFE_TEXT`、`DELTA_MISMATCH`、`FRAGMENT_MISMATCH`。

**后果**
- Agent 不能提交原始文本；复杂交易形态（成本、批次）必须先扩展 schema。
- 多一次加载比对，成本很小。
- 依赖 P0-A 确认 entry 比较方式。

**验收**
- T17：narration 含换行和 `2026-01-01 open …` → `UNSAFE_TEXT` 阻断。
- T18：payee 含 `"` 与 `\` → 转义后往返比对通过，账本中的文本等于原值。
- T19：模拟渲染器缺陷使加载后多出一条 entry → `DELTA_MISMATCH`。
- T20：validation 之后篡改待写入片段 → `FRAGMENT_MISMATCH`。
