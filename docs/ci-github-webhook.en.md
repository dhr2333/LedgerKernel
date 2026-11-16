English | [中文](ci-github-webhook.md)

# Automated builds from GitHub webhooks (Jenkins)

CI for this repository is triggered by an **internal Jenkins** (`https://jenkins.dhr2333.cn/job/LedgerKernel/`) through a **GitHub webhook**. Jenkins reports the result back as a **commit status** with the context `continuous-integration/jenkins/branch`; builds originating from a PR also appear in that PR's checks area.

This document covers both sides: the Jenkins server configuration, the GitHub repository configuration, and how the maintainer's local credentials are rotated. Read this section before changing either side.

## 0. How it works and what it does not do

```
push / pull_request ──webhook──▶ Jenkins /github-webhook/ ──▶ Multibranch job
                                                                  │
                                        Jenkinsfile:pipeline ◀─────┘
                                                                  │
              commit status continuous-integration/jenkins/branch ◀── written by the Git plugin
```

- The Jenkinsfile does one thing: inside a disposable `python:3.12-slim` container it runs `pip install -e .`, the full `unittest` suite, and a `ledger-kernel --version/--help` smoke test.
- It does **not** run `scripts/e2e_testassets.py`: the E2E script needs a sanitized copy of a real ledger, which CI does not have. Say so in the PR, as required by `AGENTS.md`.
- The build does not create `.ledger-kernel/` or write to any ledger; the only artifact is the build log, and `cleanWs()` removes the workspace.
- The repository contains no Jenkins or GitHub credentials; this repository is public.

## 1. Jenkins server configuration

### 1.1 Required plugins

| Plugin | Purpose |
| --- | --- |
| Git | checkout, reporting status per commit |
| GitHub | parsing webhook payloads, identifying PR origins |
| GitHub Branch Source | discovering branches and PRs for the Multibranch Pipeline |
| Pipeline / Workflow aggregate plugins | installed by default |
| Credentials Binding | binding GitHub access credentials |

Docker is a hard dependency of the build machine: the Pipeline streams the workspace into a `python:3.12-slim` container with `tar`, so the Jenkins agent itself does not need Python.

### 1.2 GitHub credentials for writing commit statuses

- In **Manage Jenkins → Credentials → System → Global credentials**, add a **Username with password (PAT)** or **GitHub App** credential.
- Minimum PAT scopes for a public repository: `public_repo` (write commit statuses) plus `admin:repo_hook` (only when Jenkins manages the webhook).
- The credential is used only for status reporting and hook management, not for checkout (public repositories are readable anonymously).

### 1.3 Global configuration

**Manage Jenkins → System → GitHub**:

- Add a GitHub Server with API URL `https://api.github.com` and the credential from 1.2;
- If Jenkins manages hooks (see 2.2), enable **Manage hooks**;
- Save after **Test connection** succeeds.

### 1.4 Multibranch Pipeline job

| Setting | Value |
| --- | --- |
| Name | `LedgerKernel` |
| Branch Sources → GitHub | repository `dhr2333/LedgerKernel`, credential from 1.2, Behaviors: *Discover branches* and *Discover pull requests from origin* |
| Build Configuration | by Jenkinsfile, Script Path `Jenkinsfile` |
| Scan Multibranch Pipeline Triggers | recommended (e.g. once a day) as a fallback when webhook deliveries are lost |
| Orphaned Item Strategy | keep 10 days / 10 items |

Branch Source decides which branches and PRs are built: the webhook hands every push to Jenkins, but a build is only created for a branch or PR that Branch Source has discovered.

## 2. GitHub repository configuration

### 2.1 Webhook

**Repository → Settings → Webhooks → Add webhook**:

| Field | Value |
| --- | --- |
| Payload URL | `https://jenkins.dhr2333.cn/github-webhook/` (the **trailing slash is required**) |
| Content type | `application/x-www-form-urlencoded` |
| Secret | empty, or the same shared secret configured in Jenkins *GitHub* settings (see 2.3) |
| SSL verification | Enable |
| Which events | *Let me select individual events*: **Pushes** and **Pull requests** |
| Active | checked |

GitHub immediately delivers one `ping`; **Recent Deliveries** should show `200`. Correlate `X-GitHub-Delivery` with the Jenkins system log when debugging.

### 2.2 Jenkins-managed hooks (optional)

Available only when **Manage hooks** is enabled in 1.3 and the matching credential is configured. Jenkins synchronizes hooks to the repository periodically; for a single repository, configuring the hook by hand (2.1) is more transparent.

### 2.3 Shared secret and filtering (optional)

- **Shared secret**: after setting the secret in Jenkins' global GitHub configuration, put the same value in the GitHub webhook Secret field; mismatched requests get 403.
- **Event filtering**: *Branch Source → Filter by name* can be limited to `main` so that pushes to every fork do not consume build capacity.
- **Documentation changes are not skipped**: pushing docs runs the full test suite on purpose — the commands and script names in the docs are part of what is verified.

### 2.4 Branch protection (recommended)

**Settings → Branches → Branch protection rules → main**:

- *Require status checks to pass* → select **`continuous-integration/jenkins/branch`** (this is a commit status, not a Checks API check; GitHub lists it under that name).

Note: the context is generated by the Jenkins Git plugin; the actual value for this project is `continuous-integration/jenkins/branch`. Renaming the job or branch may change the context, so update the branch protection rule at the same time.

## 3. Verification checklist

- [ ] Push to `main` or open a PR: a Jenkins build appears within a minute and targets the right commit.
- [ ] The commit page shows the `continuous-integration/jenkins/branch` status, turning ✅/❌ when the build finishes.
- [ ] Force one failing test (do not merge): the status turns ❌ and the Jenkins console shows the failing case.
- [ ] **Settings → Webhooks → Recent Deliveries** shows `200` with no retry storm.
- [ ] Pushing while Jenkins is down does not affect the repository; the periodic scan catches up afterwards.

## 4. Credentials and rotation (maintainer's machine)

The CI server needs credentials, but **no token ever enters the repository**. The maintainer and agents share one rotatable Jenkins API token:

| Location | Content | Permissions |
| --- | --- | --- |
| `~/.config/jenkins-ai/token` | current token (machine-readable copy) | `0600` |
| `~/.config/jenkins-ai/config.json` | Jenkins URL, user, token name and UUID | `0600` |
| `~/.config/jenkins-ai/rotation.log` | rotation audit trail (UUIDs only, no plaintext) | `0600` |
| private note `Secret/Jenkins AI Token.md` | current token (human/agent reading area) | git-ignored in the vault, synced by Syncthing only |

Tool: `jk-token` (installed at `~/.local/bin/jk-token`).

```bash
jk-token status          # verify the current token and list the account's tokens
jk-token rotate          # mint a new token → persist and verify it → then revoke the old one
jk-token revoke-others   # revoke every token except the current one
```

Rules:

- **Rotate after every task**, and immediately if the token appears in chat, a log, or a commit message; a revoked token stops working right away (401).
- Rotation is fail-closed: the new token is written and verified before the old one is revoked; any failed step keeps the old token and exits non-zero.
- For stronger isolation, create a low-privilege Jenkins user for read-only tasks instead of reusing the administrator token.

## 5. Troubleshooting

| Symptom | Check |
| --- | --- |
| Webhook returns 404 | Payload URL must be `/github-webhook/` (with the trailing slash); make sure the reverse proxy allows that path |
| Webhook returns 403 | Secret does not match the Jenkins configuration |
| 200 but no build | Branch Source did not discover that branch/PR; check the job scan log |
| Build fails on docker | docker is unavailable on the build machine, or the agent user lacks docker permission |
| No commit status | The Git plugin credential lacks status permission, or the job has no GitHub Branch Source |
| The same commit builds twice | Normal overlap between the periodic scan and webhook deliveries |

## 6. Maintenance principles

- Changing the trigger scope (branches, events, filtering) is a CI behavior change: update this document and `docs/release-checklist.md` together.
- Do not add Pipeline steps that need credentials, access real ledgers, or write into the user's home directory.
- Credentials must never appear in build or webhook logs; they are injected only through Jenkins Credentials.
