# GitHub Webhook 自动构建（Jenkins）

> 本文为 CI 运维文档，目前只提供中文（与 `docs/design/` 同类，见 `CONTRIBUTING.md` 语言约定）。

本仓库的 CI 由**内部 Jenkins**（`https://jenkins.dhr2333.cn/`）通过 **GitHub webhook** 触发。构建结果由 Jenkins Git 插件回写到对应 commit 的 GitHub Check（名称 `LedgerKernel CI`）；来自 PR 的构建会显示在 PR 的 checks 区域。

本文档同时列出服务端（Jenkins）与仓库端（GitHub）的配置，修改任一侧前先读本节。

## 0. 原理与边界

```
push / pull_request  ──webhook──▶  Jenkins /github-webhook/  ──▶  Multibranch job
                                                                        │
                                              Jenkinsfile:pipeline ◀─────┘
                                                                        │
                                            commit check "LedgerKernel CI" ◀── Git 插件回写
```

- Jenkinsfile 只做一件事：在一次性 `python:3.12-slim` 容器中 `pip install -e .`、跑全部 `unittest`、做 `ledger-kernel --version/--help` 冒烟。
- **不**执行 `scripts/e2e_testassets.py`：E2E 需要脱敏真实账本副本，CI 中没有该资产，按 `AGENTS.md` 在 PR 中说明跳过原因。
- 构建过程不产生 `.ledger-kernel/`、不写任何账本；产物只有 `cleanWs()` 清理后的构建日志。
- 本仓库为公开仓库，webhook 配置里**不得**出现 PAT、私钥或与账本相关的凭据；CI 只需要 Jenkins 侧的 GitHub 凭据（由管理员保管）。

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

### 1.2 GitHub 访问凭据

- 进入 **Manage Jenkins → Credentials → System → Global credentials**。
- 新增类型为 **GitHub App** 或 **Username with password（PAT）** 的凭据，建议 id：`github-dhr2333`。
- 使用 PAT 时最小权限：公开仓库只需 `public_repo`（写 commit status）；若要接收私有仓库事件，再加 `repo`。
- 公开仓库的 checkout 本身不需要凭据；凭据主要用于回写 commit status。只读 fork 不会被自动部署，避免使用带写权限的高权限 token 共享给所有人。

### 1.3 全局安全配置

**Manage Jenkins → System → GitHub**：

- 勾选 **Manage hooks**（如果由 Jenkins 管理 webhook，见 2.2 的可选方式）；
- 添加 GitHub Server，URL 为 `https://api.github.com`，凭据选 1.2 中的凭据；
- **Test connection** 通过后保存。

> 若组织策略不允许 Jenkins 管理 hook，跳过勾选，按第 2 节在 GitHub 仓库中手工添加。

### 1.4 Multibranch Pipeline Job

新建 **Multibranch Pipeline**（或使用 GitHub Organization Folder 发现整个组织），配置要点：

| 配置项 | 值 |
| --- | --- |
| Branch Sources → GitHub | 仓库 `dhr2333/LedgerKernel`，凭据 1.2，**Behaviors** 勾选 *Discover branches*（或 *Filter by name: main*）与 *Discover pull requests from origin*（*Merging the pull request with the current target branch* 或 *Head*） |
| Build Configuration → Mode | by Jenkinsfile，脚本路径 `Jenkinsfile` |
| Build Configuration → Script Path | `Jenkinsfile` |
| Branch Source → Property Strategy | *All branches get the same properties*（可选，见 2.3） |
| Scan Multibranch Pipeline Triggers | 建议勾选，周期性扫描（如 1 天）作为 webhook 丢失的兜底 |
| Orphaned Item Strategy | 保留 10 天/10 个条目 |

分支 / PR / tag 规则由这里决定：webhook 会把任意推送交给 Jenkins，但只有被 Branch Source 发现的分支/PR 才会真正触发构建。

## 2. GitHub 仓库端配置

### 2.1 手工添加 webhook（推荐，公开仓库）

**仓库 → Settings → Webhooks → Add webhook**：

| 字段 | 值 |
| --- | --- |
| Payload URL | `https://jenkins.dhr2333.cn/github-webhook/`（**末尾斜杠不可少**） |
| Content type | `application/x-www-form-urlencoded` |
| Secret | 空，或与 Jenkins *GitHub* 配置中的共享密钥一致（见 2.3） |
| SSL verification | Enable（Jenkins 使用有效证书时） |
| Which events | *Let me select individual events*：**Pushes**、**Pull requests**（如需 tag 构建再加 **Branch or tag creation**） |
| Active | 勾选 |

添加后 GitHub 会立即投递一次 `ping`，Jenkins 侧对应 job 应出现一次构建（或至少 `github-webhook/` 返回 200）。在 **Recent Deliveries** 中可查看响应体，用 `X-GitHub-Delivery` 与 Jenkins 系统日志对齐排查。

### 2.2 由 Jenkins 自动管理 hook（可选）

只在 1.3 勾选 **Manage hooks** 并使用 GitHub App / PAT 凭据时可用。Jenkins 周期性同步 hook 到仓库，适合组织内多个仓库统一管理；单仓库场景手工配置更透明，推荐 2.1。

### 2.3 共享密钥与过滤（可选）

- **共享密钥**：在 Jenkins 全局 GitHub 配置中填写 secret 后，在 GitHub webhook 的 Secret 字段填同一个值；不匹配的请求返回 403，防止伪造推送触发构建。
- **事件过滤**：在 job 的 *Branch Source → Filter by name* 中只保留 `main`，或把 *Discover branches* 设为 *Only branches that are also filed as PRs*，避免每个 fork 推送都消耗构建机。
- **不相关改动跳过**：仓库默认不配置 `[skip ci]` 语义；推送文档到 `main` 同样会触发全量测试，这是刻意选择（文档中的命令与脚本名也是被测试的对象）。

### 2.4 分支保护（可选但推荐）

**Settings → Branches → Branch protection rules → main**：

- *Require status checks to pass* → 选择 `LedgerKernel CI`；
- 只有该 check 通过才允许合并，使“webhook 构建为绿”成为合并门禁，与 `CONTRIBUTING.md` 的“合并前 `main` 构建为绿”一致。

注意：check 名称来自 Jenkins Git 插件（默认 `<job 名>`），改名会同时影响状态选择项。

## 3. 验证清单

- [ ] 推送一个提交到 `main`（或提一个 PR）：Jenkins 在 1 分钟内出现构建，构建对象与 commit 正确。
- [ ] GitHub commit 页面出现 `LedgerKernel CI` check，构建结束后变为 ✅/❌。
- [ ] 故意让一次测试失败（PR 中不要合入）：check 变 ❌，Jenkins 构建日志显示失败用例。
- [ ] 在 **Settings → Webhooks → Recent Deliveries** 看到 `200`/`204` 响应，无重试风暴。
- [ ] 断网/停 Jenkins 后推送不受影响（GitHub 只做重试），恢复后由周期性扫描兜底。

## 4. 排障

| 现象 | 检查 |
| --- | --- |
| webhook 返回 404 | Payload URL 是否为 `/github-webhook/`（含末尾斜杠）；Jenkins 是否经反代暴露且允许该路径 |
| webhook 返回 403 | Secret 与 Jenkins 配置不一致 |
| 200 但无构建 | job 未包含该分支/PR（Branch Source 过滤）；查看 job 扫描日志 |
| 构建失败于 docker | 构建机 docker 不可用，或 agent 用户无 docker 权限 |
| 无 commit check | Git 插件凭据缺少 status 权限；或 job 未配置 GitHub Branch Source |
| 同一 commit 重复构建 | 周期性扫描与 webhook 叠加属正常；可缩短扫描间隔以外不做额外去重 |

## 5. 维护原则

- 修改触发范围（分支、事件、过滤）属于 CI 行为变化：同步更新本文与 `docs/release-checklist.md`。
- 不在 Pipeline 中新增需要凭据的测试、网络访问真实账本或写入用户目录的步骤。
- 公开仓库的 webhook 日志、构建日志中不得出现凭据；凭据只通过 Jenkins Credentials 注入。
