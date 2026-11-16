中文 | [English](ci-github-webhook.en.md)

# GitHub Webhook 自动构建（Jenkins）

本仓库的 CI 由**内部 Jenkins**（`https://jenkins.dhr2333.cn/job/LedgerKernel/`）通过 **GitHub webhook** 触发。构建结果由 Jenkins Git 插件以 **commit status** 的形式回写，context 为 `continuous-integration/jenkins/branch`；来自 PR 的构建会显示在 PR 的 checks 区域。

本文档同时列出服务端（Jenkins）与仓库端（GitHub）的配置，以及维护者本地凭据的轮换方式。修改任一侧前先读本节。

## 0. 原理与边界

```
push / pull_request ──webhook──▶ Jenkins /github-webhook/ ──▶ Multibranch job
                                                                  │
                                        Jenkinsfile:pipeline ◀─────┘
                                                                  │
              commit status continuous-integration/jenkins/branch ◀── Git 插件回写
```

- Jenkinsfile 只做一件事：在一次性 `python:3.12-slim` 容器中 `pip install -e .`、跑全部 `unittest`、做 `ledger-kernel --version/--help` 冒烟。
- **不**执行 `scripts/e2e_testassets.py`：E2E 需要脱敏真实账本副本，CI 中没有该资产，按 `AGENTS.md` 在 PR 中说明跳过原因。
- 构建过程不产生 `.ledger-kernel/`、不写任何账本；产物只有 `cleanWs()` 清理后的构建日志。
- 仓库内不含任何 Jenkins / GitHub 凭据；本仓库是公开仓库。

## 1. Jenkins 服务端配置

### 1.1 必需插件

| 插件 | 用途 |
| --- | --- |
| Git | checkout、按 commit 回写 status |
| GitHub | 解析 webhook payload，PR 来源识别 |
| GitHub Branch Source（多分支） | Multibranch Pipeline 自动发现分支与 PR |
| Pipeline / Workflow 聚合插件 | 默认安装即含 |
| Credentials Binding | 绑定 GitHub 访问凭据 |

Docker 是构建机的硬依赖：Pipeline 通过 `tar` 把工作区送入 `python:3.12-slim` 容器，Jenkins agent 本身不需要安装 Python。

### 1.2 回写 commit status 的 GitHub 凭据

- **Manage Jenkins → Credentials → System → Global credentials** 中新增 **Username with password（PAT）**，或 **GitHub App** 凭据。
- PAT 最小权限：公开仓库只需 `public_repo`（写 commit status）+ `admin:repo_hook`（仅在由 Jenkins 管理 webhook 时需要）。
- 该凭据只用于 Jenkins 回写状态与管理 hook，不用于 checkout（公开仓库匿名可读）。

### 1.3 全局配置

**Manage Jenkins → System → GitHub**：

- 添加 GitHub Server，API URL 为 `https://api.github.com`，凭据选 1.2；
- 若采用 Jenkins 管理 hook（见 2.2），勾选 **Manage hooks**；
- **Test connection** 通过后保存。

### 1.4 Multibranch Pipeline Job

| 配置项 | 值 |
| --- | --- |
| 名称 | `LedgerKernel` |
| Branch Sources → GitHub | 仓库 `dhr2333/LedgerKernel`，凭据取自 1.2，Behaviors 勾选 *Discover branches* 与 *Discover pull requests from origin* |
| Build Configuration | by Jenkinsfile，Script Path `Jenkinsfile` |
| Scan Multibranch Pipeline Triggers | 建议勾选（如 1 天一次）作为 webhook 丢失时的兜底 |
| Orphaned Item Strategy | 保留 10 天 / 10 个条目 |

分支与 PR 的触发范围由 Branch Source 决定：webhook 会把任意推送交给 Jenkins，但只有被 Branch Source 发现的分支/PR 才会真正创建构建。

## 2. GitHub 仓库端配置

### 2.1 Webhook

**仓库 → Settings → Webhooks → Add webhook**：

| 字段 | 值 |
| --- | --- |
| Payload URL | `https://jenkins.dhr2333.cn/github-webhook/`（**末尾斜杠不可少**） |
| Content type | `application/x-www-form-urlencoded` |
| Secret | 空，或与 Jenkins *GitHub* 配置中的共享密钥一致（见 2.3） |
| SSL verification | Enable |
| Which events | *Let me select individual events*：**Pushes**、**Pull requests** |
| Active | 勾选 |

添加后 GitHub 会立即投递一次 `ping`，`Recent Deliveries` 中应看到 `200`。用 `X-GitHub-Delivery` 与 Jenkins 系统日志对齐排查。

### 2.2 由 Jenkins 自动管理 hook（可选）

只在 1.3 勾选 **Manage hooks** 并使用相应凭据时可用。Jenkins 周期性同步 hook 到仓库；单仓库场景手工配置（2.1）更透明。

### 2.3 共享密钥与过滤（可选）

- **共享密钥**：Jenkins 全局 GitHub 配置填写 secret 后，GitHub webhook 的 Secret 填同一个值；不匹配的请求返回 403。
- **事件过滤**：job 的 *Branch Source → Filter by name* 可只保留 `main`，避免每个 fork 推送都占用构建机。
- **不跳过文档改动**：推送文档同样触发全量测试，这是刻意选择——文档里的命令与脚本名也在被验证的范围内。

### 2.4 分支保护（推荐）

**Settings → Branches → Branch protection rules → main**：

- *Require status checks to pass* → 选择 **`continuous-integration/jenkins/branch`**（这是 commit status，不是 Checks API；在 GitHub 的检查列表里显示为 `continuous-integration/jenkins/branch`）。

注意：该 context 由 Jenkins Git 插件生成，本项目实际值为 `continuous-integration/jenkins/branch`；重命名 job 或分支可能改变 context，需要同步更新分支保护规则。

## 3. 验证清单

- [ ] 推送到 `main` 或提 PR：Jenkins 在 1 分钟内出现构建，构建对象与 commit 一致。
- [ ] commit 页面出现 `continuous-integration/jenkins/branch` 状态，构建结束后变为 ✅/❌。
- [ ] 让一次测试失败（不要合入）：状态变 ❌，Jenkins 控制台显示失败用例。
- [ ] **Settings → Webhooks → Recent Deliveries** 显示 `200`，无重试风暴。
- [ ] Jenkins 停机期间推送不影响仓库；恢复后由周期性扫描补建。

## 4. 凭据与轮换（维护者本地）

CI 服务端需要凭据，但**任何令牌都不进入仓库**。维护者与 AI 共用一份可轮换的 Jenkins API 令牌：

| 位置 | 内容 | 权限 |
| --- | --- | --- |
| `~/.config/jenkins-ai/token` | 当前令牌（机器可读副本） | `0600` |
| `~/.config/jenkins-ai/config.json` | Jenkins URL、用户、令牌名与 UUID | `0600` |
| `~/.config/jenkins-ai/rotation.log` | 轮换审计（只记 UUID，不记明文） | `0600` |
| 私有笔记 `Secret/Jenkins AI Token.md` | 当前令牌（人类/AI 读取区） | vault 已 `.gitignore`，仅 Syncthing 同步 |

工具：`jk-token`（位于 `~/.local/bin/jk-token`）。

```bash
jk-token status          # 验证当前令牌并列出账号下的令牌
jk-token rotate          # 生成新令牌 → 落盘并校验 → 再吊销旧令牌
jk-token exec -- <cmd>   # 把令牌注入 JENKINS_TOKEN 等环境变量执行命令，结束后自动轮换
jk-token revoke-others   # 吊销除当前令牌以外的所有令牌
```

需要“每次使用后自动换令牌”时，用 `jk-token exec -- <命令>`：命令拿到 `JENKINS_URL` / `JENKINS_USER` / `JENKINS_TOKEN`，执行完毕（无论成败）都会轮换并吊销旧令牌。

约定：

- **每次任务结束后轮换**，或一旦令牌出现在聊天、日志、提交信息中立即轮换；旧令牌吊销后立即失效（401）。
- 轮换是失败关闭的：新令牌写入并验证通过后才吊销旧的；任一环节失败则保留旧令牌并非零退出。
- 需要更高隔离时，为只读任务单独创建低权限 Jenkins 用户，不要复用管理员令牌。

## 5. 排障

| 现象 | 检查 |
| --- | --- |
| webhook 返回 404 | Payload URL 是否为 `/github-webhook/`（含末尾斜杠）；反代是否放行该路径 |
| webhook 返回 403 | Secret 与 Jenkins 配置不一致 |
| 200 但无构建 | 该分支/PR 未被 Branch Source 发现；查看 job 扫描日志 |
| 构建失败于 docker | 构建机 docker 不可用，或 agent 用户无 docker 权限 |
| 无 commit status | Git 插件凭据缺少 status 权限；或 job 未配置 GitHub Branch Source |
| 同一 commit 重复构建 | 周期性扫描与 webhook 叠加属正常 |

## 6. 维护原则

- 修改触发范围（分支、事件、过滤）属于 CI 行为变化：同步更新本文与 `docs/release-checklist.md`。
- 不在 Pipeline 中新增需要凭据的测试、访问真实账本或写入用户目录的步骤。
- 构建日志与 webhook 日志中不得出现凭据；凭据只通过 Jenkins Credentials 注入。
