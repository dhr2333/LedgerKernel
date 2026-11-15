# LedgerKernel 库内 Agent 约定

本文件约束在本仓库内工作的 Agent。它只写会改变行为或安全边界的规则；背景与设计见 `README.md`。

## 开工前必读

1. `README.md`：边界、接口、已知限制。
2. `CONTRIBUTING.md`：开发、测试、文档与提交约定。
3. 涉及发布时读 `docs/release-checklist.md`。

## 硬约束（不可绕过）

1. **真实账本只读**：测试、演示和开发不得修改真实账本或 `TestAssets` 原目录；必须使用临时副本。
2. **唯一受控写入入口**：生产代码的自动化写入只能通过 `Kernel`；不得在 CLI、MCP 或脚本中直接追加/重写账本文件。
3. **人工批准**：不得增加自动批准、Agent 代批准或“跳过审批”的代码路径。批准必须绑定 `draft_id + validation_id + ledger_revision + contract_revision`。
4. **审计与 journal 不可删除/改写**：不得为了“让测试通过”清理审计链、journal 或 observations；恢复只能走 `recover` 状态机。
5. **Beancount 版本固定**：当前支持 `>=3.2,<4`（开发验证 3.2.3）。升级必须跑真实账本回归并更新 `engine_fingerprint` 相关测试。
6. **不提交秘密与真实数据**：不提交真实账本、`.ledger-kernel/`、API token、PAT、私钥、含个人信息的证据文件。
7. **不削弱安全不变量**：ADR-012 增量断言、片段指纹、外部变化只观测不覆盖、失败关闭（fail-closed）等规则不得因便利性被移除。

## 文档更新规则

按“行为是否变化”触发，不做逐次强制：

- 公共 API、CLI、配置、schema、安装方式变化 → 更新 `README.md` 与相关 docstring。
- 安全不变量、不可逆决策、兼容性边界变化 → 新增/修订 ADR（本仓库 `docs/design/adr/`，或库外设计库）。
- 仅内部重构、测试补充、注释修正 → 不要求更新 README。
- 发布相关 → 更新 `CHANGELOG.md` 并按 `docs/release-checklist.md` 执行。

## 完成定义（Definition of Done）

- 新增/修改行为有对应测试；测试仍使用标准库 `unittest`。
- 本地通过：`PYTHONPATH=src python3 -m unittest discover -s tests -t .`
- 涉及写入、恢复、外部变化的改动，额外跑：`PYTHONPATH=src python3 scripts/e2e_testassets.py --source <TestAssets>`（没有 TestAssets 时说明跳过原因）。
- 公共行为变化已更新文档；决策变化已记录 ADR。
- `git status` 只包含本次相关文件；不顺手整理无关内容。
- 不提交 `.venv/`、`__pycache__/`、构建产物或临时账本。

## 代码约定

- Python 3.11+；领域层不依赖 CLI/MCP/HTTP。
- 金额用 `Decimal` 或十进制字符串，禁止让 JSON float 进入账本语义。
- 错误使用 `ledger_kernel.errors` 的稳定 code；不要用裸 `Exception` 替代。
- 保持单文件追加、单 `write_target` 的 v0.1 边界；扩展前先写 ADR。
