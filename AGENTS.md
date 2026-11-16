中文 | [English](AGENTS.en.md)

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

## 凭据使用

- CI / Jenkins 令牌等运维凭据由维护者本地保存与轮换（见 `docs/ci-github-webhook.md` 第 4 节），不进入仓库、提交信息、构建日志或示例配置。
- Agent 需要凭据时从维护者指定的固定位置读取，用完由维护者按该文档轮换；不得把令牌抄写到任何仓库文件或临时脚本中。
- 只用最小权限账号；管理员凭据仅用于文档列出的运维动作。

## 文档更新规则

按“行为是否变化”触发，不做逐次强制：

- 公共 API、CLI、配置、schema、安装方式变化 → 更新 `README.md` / `README.en.md` 与相关 docstring。
- 安全不变量、不可逆决策、兼容性边界变化 → 新增/修订 ADR，只进本仓库 `docs/design/adr/`。
- 仅内部重构、测试补充、注释修正 → 不要求更新 README。
- 发布相关 → 更新 `CHANGELOG.md` / `CHANGELOG.en.md` 并按 `docs/release-checklist.md` 执行。

### 设计文档权威

- 规范性设计（顶层设计摘要、设计规格、实施计划、ADR、实施记录）只以本仓库 `docs/design/` 为唯一真相源。
- 库外笔记（Obsidian 设计库）不保留规范正文副本，只放项目状态与未冻结讨论稿；标注为草稿的内容在被引用前先落到仓库。
- 两份副本冲突时，以能被测试或 commit 验证的那份为准，即本仓库。

### 双语同步

- 面向使用者的文档有两版：`X.md`（中文）与 `X.en.md`（英文），覆盖 `README`、`CONTRIBUTING`、`CHANGELOG`、`AGENTS`。
- 任何内容改动必须在同一次改动中同时落到两版；“以后补翻译”不是合并后可以接受的状态。
- 中文版是措辞准绳；英文版必须表达相同的规则、约束与数字，不得出现语义分歧。
- 示例中的标识（账户名、命令、ID、配置键）两版保持逐字一致，读者可以照抄任意一版。
- 新增面向使用者的文档时，同时创建两个文件，并在各自首行加语言切换行。
- Agent 自身的阅读偏好（例如只读中文）不能成为不更新英文版的理由。

## 完成定义（Definition of Done）

- 新增/修改行为有对应测试；测试仍使用标准库 `unittest`。
- 本地通过：`PYTHONPATH=src python3 -m unittest discover -s tests -t .`
- 涉及写入、恢复、外部变化的改动，额外跑：`PYTHONPATH=src python3 scripts/e2e_testassets.py --source <TestAssets>`（没有 TestAssets 时说明跳过原因）。
- 公共行为变化已在中英两版更新文档；决策变化已记录 ADR。
- `git status` 只包含本次相关文件；不顺手整理无关内容。
- 不提交 `.venv/`、`__pycache__/`、构建产物或临时账本。

## 代码约定

- Python 3.11+；领域层不依赖 CLI/MCP/HTTP。
- 金额用 `Decimal` 或十进制字符串，禁止让 JSON float 进入账本语义。
- 错误使用 `ledger_kernel.errors` 的稳定 code；不要用裸 `Exception` 替代。
- 保持单文件追加、单 `write_target` 的 v0.1 边界；扩展前先写 ADR。
