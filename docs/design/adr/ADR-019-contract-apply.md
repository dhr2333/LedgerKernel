# ADR-019：契约编辑走“验证 + CAS + 原子替换 + 审计”，不新增绕过路径

状态：[提议]

**背景**
v0.1 的账户契约只能手工编辑 `contracts.yaml`：没有校验预览、没有并发保护、没有变更审计，用户容易写坏文件或覆盖他人修改。契约是账户语义第三层的核心，不能停留在“手改 YAML”。

**决策**
1. 契约文件仍是 `.ledger-kernel/contracts.yaml`，唯一写入口是 `ledger-kernel contracts apply --file <new> --expected-current <sha> --confirm <sha>`。
2. `apply` 必须同时提供两个哈希：
   - `--expected-current`：当前文件哈希（不存在时写 `absent`），不匹配则 `CONTRACT_CONFLICT`，不写；
   - `--confirm`：新文件完整 SHA-256，防误确认。
3. 写入前严格解析并校验（未知字段、重复别名、非法账户、cadence/余额区间、`unmanaged_accounts` 重叠）；失败即 `CONTRACT_INVALID`，不写。
4. 写入使用同目录临时文件 + fsync + `os.replace` + 目录 fsync，并持有 `kernel.lock`；旧/新内容存入内容寻址 blob。
5. 追加审计 `ContractChanged {before_sha256, after_sha256, actor, note}`。
6. `contract_revision` 变化使所有 pending validation/approval 失效；沿用现有绑定规则，下一次操作时重验，不在 apply 内批量重验。
7. 不新增自动批准/自动修复路径；契约编辑不写 Ledger，只改元数据。

**后果**
- 契约变更可审计、可预览、可防并发覆盖。
- 旧契约文件无需改动即可继续加载；旧内核遇到新增字段会拒绝，升级时必须同版本使用。
- 契约编辑会让在途批准失效，这是有意为之。

**验收**
- 合法 apply、CAS 冲突、非法 schema、契约变更使批准失效、`ledger_revision` 不变（见 `v0.2-account-layer.md` 第 9 节）。
