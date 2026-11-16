English | [中文](CONTRIBUTING.md)

# Contributing to LedgerKernel

Thank you for contributing. LedgerKernel is a local ledger account kernel where correctness outweighs feature count; any change that weakens the audit, approval, or append boundaries must be discussed first.

## Development environment

Requirements:

- Python 3.11+
- Beancount 3.2.x (verified on 3.2.3)
- PyYAML 6+

```bash
git clone git@github.com:dhr2333/LedgerKernel.git
cd LedgerKernel
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

## Testing

```bash
# Unit + integration tests (required)
PYTHONPATH=src python3 -m unittest discover -s tests -t .

# Synthetic-fixture end-to-end (default; CI runs it on every build)
PYTHONPATH=src python3 scripts/e2e_ledger.py

# Real-ledger copy end-to-end (when writing/recovery/external changes are involved)
PYTHONPATH=src python3 scripts/e2e_ledger.py --source /path/to/TestAssets
```

E2E only operates on a temporary copy; the source directory given by `--source` **must not** be modified. When no sanitized TestAssets ledger is available, state in the PR that the real-ledger regression was skipped; the synthetic fixture part must still pass in CI.

## Pre-submit checks

- Tests pass; changes involving writes/recovery have crash or external-change coverage.
- Changes to the public API / CLI / configuration / schema have updated `README.md` (and `README.en.md`).
- Changes to security invariants or irreversible decisions come with a new ADR.
- No real ledgers, `.ledger-kernel/`, tokens, keys, or personal data are committed.
- Amounts are still decimal strings/`Decimal`; no JSON float is introduced as authoritative input.
- No new ledger write path bypassing `Kernel` is added.

## Commits and PRs

- Use the `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:` prefixes for commit messages.
- One PR does one thing; security boundary changes go in a separate PR.
- The PR description states: what changed, why, how it was verified, and the compatibility impact on existing drafts/audit/journal.
- Do not force-push published branches; do not rewrite other people's commits.

## Documentation and releases

- Design documents live in `docs/design/`; documentation rules are in `AGENTS.md`.
- The release process is in `docs/release-checklist.md`; version records are in `CHANGELOG.md`.
- Before deciding the v0.2 scope, collect data on a real-ledger copy following `docs/dogfood.en.md`; write the conclusion into `docs/design/`.
- CI currently runs on an internal Jenkins, triggered by a GitHub webhook on push / PR; the result appears as the `continuous-integration/jenkins/branch` commit status on commits and PRs. The `main` build must be green before merging. Webhook and job configuration: `docs/ci-github-webhook.en.md`.
- CI runs unit/integration tests, the synthetic-fixture E2E, and a CLI smoke check; the real-ledger `--source` E2E is not executed in CI. Changes involving writes/recovery must be run locally and noted in the PR.

## Language policy

- Every user-facing document ships in both Chinese and English: `X.md` is Chinese, `X.en.md` is English (`README`, `CONTRIBUTING`, `CHANGELOG`, `AGENTS`).
- A change to one language must be applied to the other in the same PR; leaving one side stale is a blocking review comment, not a follow-up task.
- Code, identifiers, CLI output, config keys, and stable error codes are English in both versions. Entity names inside Beancount examples (e.g. `Assets:Bank:Checking`) stay identical across versions.
- Design documents under `docs/design/` are Chinese-only for the v0.1 prototype baseline; translate them only when they become a long-term product commitment.

## Security boundaries

- The threat model and limitations are in the "Known limitations" section of `README.md`; do not advertise the project as "an Agent cannot bypass the ledger".
- The user keeps the highest authority to edit the ledger directly; external changes can only be discovered and recorded, never silently overwritten by the kernel.
- Plugins are a code execution environment trusted by the user; an Agent cannot specify plugins or load paths through a draft.
