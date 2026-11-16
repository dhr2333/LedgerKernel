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

# 合成 fixture 端到端（默认，CI 每次构建都跑）
PYTHONPATH=src python3 scripts/e2e_ledger.py

# 真实账本副本端到端（涉及写入/恢复/外部变化时）
PYTHONPATH=src python3 scripts/e2e_ledger.py --source /path/to/TestAssets
```

E2E 只操作临时副本；`--source` 指向的原目录**不得**被修改。没有脱敏 TestAssets 账本时在 PR 中说明跳过真实账本回归，合成 fixture 部分仍必须在 CI 中通过。

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

- 设计文档见 `docs/design/`；文档规则见 `AGENTS.md`。
- 发布流程见 `docs/release-checklist.md`；版本记录见 `CHANGELOG.md`。
- v0.2 范围决策前，按 `docs/dogfood.md` 在真实账本副本上收集数据；结论写入 `docs/design/`。
- 当前 CI 为内部 Jenkins，由 GitHub webhook 在 push / PR 时触发；结果以 commit status `continuous-integration/jenkins/branch` 显示在 commit 和 PR 上。合并前至少保证 `main` 构建为绿。webhook 与 job 配置见 `docs/ci-github-webhook.md`。
- CI 跑单元/集成测试、合成 fixture E2E 与 CLI 冒烟；真实账本的 `--source` E2E 不在 CI 中执行，涉及写入/恢复的改动需在本地跑并在 PR 中注明。

## 安全边界

- 本项目的威胁模型与限制写在 `README.md` 的“已知限制”；不要把它宣传成“Agent 无法绕过账本”。
- 用户保留直接编辑账本的最高权限；外部变化只能被发现和记录，不能被内核静默覆盖。
- 插件是用户信任的代码执行环境；Agent 不能通过草稿指定插件或加载路径。
