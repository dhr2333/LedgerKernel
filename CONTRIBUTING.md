# Contributing to LedgerKernel

感谢参与。LedgerKernel 是本地账本账户内核，正确性优先于功能数量；任何会削弱审计、批准或追加边界的改动都需要先讨论。

## 开发环境

要求：

- Python 3.11+
- Beancount 3.2.x（开发验证 3.2.3）
- PyYAML 6+

```bash
git clone git@github.com:dhr2333/LedgerKernel.git
cd LedgerKernel
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

## 测试

```bash
# 单元 + 集成测试（必须）
PYTHONPATH=src python3 -m unittest discover -s tests -t .

# 真实账本副本端到端（涉及写入/恢复/外部变化时）
PYTHONPATH=src python3 scripts/e2e_testassets.py
```

E2E 只操作 `/home/dhr2333/Syncthing/Bill/TestAssets` 的临时副本；**不得**修改原目录。没有该账本时在 PR 中说明跳过原因。

## 提交前检查

- 测试通过；涉及写入/恢复的改动有崩溃或外部变化场景覆盖。
- 公共 API / CLI / 配置 / schema 变化已更新 `README.md`。
- 安全不变量或不可逆决策变化已新增 ADR。
- 未提交真实账本、`.ledger-kernel/`、token、私钥、个人信息。
- 金额仍是十进制字符串/`Decimal`，没有引入 JSON float 作为权威输入。
- 没有新增绕过 `Kernel` 的账本写入路径。

## 提交与 PR

- 提交信息用 `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:` 前缀。
- 一个 PR 只做一件事；安全边界改动单独 PR。
- PR 描述写清：改了什么、为什么、如何验证、对现有草稿/审计/journal 的兼容性影响。
- 不要 force-push 已发布分支；不要重写他人提交。

## 文档与发布

- 文档规则见 `AGENTS.md`。
- 发布流程见 `docs/release-checklist.md`；版本记录见 `CHANGELOG.md`。
- 当前 CI 为 Jenkins：<https://jenkins.dhr2333.cn/job/LedgerKernel/>。合并前至少保证 `main` 构建为绿。

## 安全边界

- 本项目的威胁模型与限制写在 `README.md` 的“已知限制”；不要把它宣传成“Agent 无法绕过账本”。
- 用户保留直接编辑账本的最高权限；外部变化只能被发现和记录，不能被内核静默覆盖。
- 插件是用户信任的代码执行环境；Agent 不能通过草稿指定插件或加载路径。
