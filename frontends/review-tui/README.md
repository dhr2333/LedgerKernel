中文 | [English](README.en.md)

# LedgerKernel 审核 TUI（ledger-kernel-review-tui）

LedgerKernel v0.3 的外置审核前端骨架：一个标准库 curses 极简 TUI，把内核已有的只读审核视图与逐草稿人工批准流程串起来。它是**同仓库、内核之外的独立安装单元**，内核包不依赖、不默认安装它。

## 用途

- 按账户分组浏览待审草稿（来自 `ledger-kernel review --by-account --json` 的只读视图）。
- 查看选中草稿的详情：待追加片段（fragment）、校验项（checks）、diff 摘要。
- 对选中草稿执行 `ledger-kernel drafts validate`，然后要求人工输入与 CLI 相同的确认串，再调用 `ledger-kernel drafts approve --confirm`。
- `--list` 提供非交互摘要，便于 SSH、脚本或无 curses 的环境。
- 不修改内核代码，不新增写入路径。

## 边界

- **只读 + 现有 CLI 写入**：读取只用 `review --json` 与 `review <draft_id> --json`；写入只用已存在的 `drafts validate` 和 `drafts approve --confirm`。
- **不 import 内核**：源码不出现 `ledger_kernel` 导入（有边界测试扫描）；只消费内核 CLI 的 JSON 契约。
- **无服务端、无网络监听、无账号体系**：本地单用户，不新增网络 I/O。
- **无自动批准、无跳过确认、无账户级批量批准**：一次只批准光标选中的单个草稿；必须在 TUI 中手动逐字输入 draft_id，输入不一致立即取消且不调用内核。
- **不复制内核校验逻辑**：validate 完全由 `ledger-kernel` 执行，前端只展示结果。
- 批准绑定（`draft_id + validation_id + ledger_revision + contract_revision`）仍由内核保证，前端不参与。
- POSIX-only（标准库 curses），Windows 不在 v0.3 范围（见 `docs/design/v0.3-scope.md` D05）。

## 依赖与安装

- Python 3.11+
- 已安装并初始化的 `ledger-kernel` CLI（v0.2.0；本前端只依赖其审核 JSON 字段）
- 无运行时第三方依赖

从仓库根目录安装（独立包，不影响内核包）：

```bash
python3 -m pip install ./frontends/review-tui
# 开发模式
python3 -m pip install -e ./frontends/review-tui
```

不安装、直接从源码运行：

```bash
PYTHONPATH=frontends/review-tui/src python3 -m ledger_review_tui --ledger /path/to/ledger
```

## 运行

```bash
# 默认进入 curses TUI（账本目录需已有 .ledger-kernel/ 状态）
ledger-review-tui --ledger /path/to/ledger
ledger-review-tui --ledger /path/to/ledger --kernel-cmd ledger-kernel

# 非交互：打印按账户分组的待审摘要后退出
ledger-review-tui --ledger /path/to/ledger --list
```

`--list` 输出示例（格式示意，不是真实账本数据）：

```text
== Assets:Bank  open  1000.00 CNY  reconciliation current
   drf_abc123  2026-11-15  120.00 CNY  Books  [NeedsHumanApproval / pass]
   warning: CONTRACT_CURRENCY  currency not allowed  [drf_abc123]
== Expenses:Books  open  -120.00 CNY  reconciliation not_configured
   drf_def456  2026-11-16  35.00 CNY  Lunch  [Submitted / unvalidated]
```

## TUI 键位与确认流程

| 按键 | 动作 |
| --- | --- |
| `↑` / `↓`（或 `k` / `j`） | 在账户分组与草稿行之间移动 |
| `Enter` | 查看选中草稿详情（fragment / checks / diff，只读分页） |
| `v` | 对选中草稿执行 `drafts validate`，显示校验结果 |
| `a` | 批准选中草稿，见下方确认流程 |
| `r` | 重新读取 `review --by-account` |
| `q` | 退出 |

批准流程（逐草稿）：

1. 光标停在草稿行（不是账户分组行），按 `a`；
2. 底部提示 `type exactly 'drf_…' to approve`；
3. 手动逐字输入 draft_id 后回车；
4. 只有与 CLI 一致的确认串（单草稿即其 draft_id）才继续；输入不一致立即取消，不调用内核；
5. 前端调用 `drafts approve <draft_id> --confirm <draft_id>`，成功后刷新列表。

底部状态行显示最近操作或内核错误；失败不会自动重试。

## 开发与测试

标准库 `unittest`；测试不启动真实内核、不依赖交互终端：

```bash
PYTHONPATH=frontends/review-tui/src python3 -m unittest discover -s frontends/review-tui/tests -t frontends/review-tui
```

覆盖：命令拼接与 JSON 解析（FakeRunner）、CLI 错误到异常的映射、approve 必须显式且精确的 confirm（不一致时不调用内核）、分组/选择/确认串/详情渲染、`--list` 输出（注入 fake 数据，不启动子进程）、以及源码不 import `ledger_kernel` 的边界扫描。

## 已知限制

- 一次只批准一个选中的草稿；没有多选、没有账户级一键批准（这是安全边界，不是待办）。
- 不提供 reject / withdraw / commit / scan / 契约编辑 / 对账入口；这些仍用 CLI。
- 不缓存、不监听文件变化；`r` 手动刷新，外部变化由内核在调用时检测。
- 无颜色主题、无鼠标、无搜索/过滤；终端过小时只显示提示，不崩溃。
- 依赖内核 CLI 的只读 JSON 契约；字段变更需要同步本包。
- 仅 POSIX（curses）；Windows 不在 v0.3 范围。
- 骨架未做 PyPI 发布与独立版本策略，也未在 CI 中固定 `ledger-kernel` 版本区间。

## 相关文档

- 范围与非目标：`docs/design/v0.3-scope.md`
- 内核审核流程与 JSON 面：仓库根 `README.md`
- 安全不变量：仓库根 `README.md` 与 `AGENTS.md`
