English | [中文](README.md)

# LedgerKernel v0.1

A local ledger account kernel that wraps a Beancount ledger and provides a closed loop of **proposal → full dry-run → human approval → controlled append → audit → recovery**.

The ledger file is the single source of truth for financial facts; the kernel is the only **controlled entry point for automation writes**. The user keeps the highest authority to edit the ledger directly — the kernel does not block or overwrite edits, it only discovers and records them, and marks changes it cannot safely attribute as `external_unknown`.

> This repository is the v0.1 prototype implementation. The design baseline — *Design Summary*, *Engineering Decision Freeze and Prototype Implementation Plan*, *Design Spec Draft*, implementation report, and ADR-012/ADR-013 — has exactly one source of truth: [`docs/design/`](docs/design/README.md). External notes keep no copy of it. The implementation covers P0-A to P0-D plus a thin MCP adapter (the minimal tool set of P0-E).

CI: Jenkins (internal, triggered by GitHub webhook; configuration in [ci-github-webhook](docs/ci-github-webhook.en.md)) · Development conventions: [CONTRIBUTING.en.md](CONTRIBUTING.en.md) · Agent conventions: [AGENTS.en.md](AGENTS.en.md) · Release process: [release-checklist](docs/release-checklist.md) · Changelog: [CHANGELOG.en.md](CHANGELOG.en.md)

This document is available in both Chinese and English: `X.md` is Chinese and `X.en.md` is English; both must be updated in the same change (see `AGENTS.md`).

## What this is not

- Not a bookkeeping app, budgeting tool, or spending dashboard.
- No built-in AI reasoning and no model binding; the Agent lives outside the process and only submits structured drafts.
- No promise that "an Agent can never write the ledger directly". Under the same OS user, an Agent cannot be reliably distinguished from the user; direct writes are treated as external changes.
- Git history is not an audit trail, and a hash is not authentication.
- v0.1 has no automatic review tiering: every draft requires human approval.

## Quick start

Requires Python ≥ 3.11 and Beancount 3.2.x.

```bash
git clone git@github.com:dhr2333/LedgerKernel.git
cd LedgerKernel
python3 -m pip install -e .
# or skip installation and use PYTHONPATH=src

ledger-kernel init /path/to/ledger/main.bean \
  --write-target transactions/kernel-import.bean

ledger-kernel status
ledger-kernel accounts list
ledger-kernel accounts show Assets:Cash
ledger-kernel accounts balance Assets:Cash --as-of 2026-11-15
```

`write_target` must:

- be a normalized relative path inside the ledger directory;
- really exist and not be a symbolic link;
- actually be loaded by the root ledger's `include` closure;
- use UTF-8 (BOM allowed); the newline style is preserved when appending.

`init` creates `.ledger-kernel/` and writes the initial revision, audit chain head, and an empty contract file. If the directory is already bound, it refuses to overwrite.

## Proposal and review loop

The Agent submits structured JSON only, never raw Beancount text. Example:

```json
{
  "schema_version": 1,
  "operation": "append_transactions",
  "transactions": [
    {
      "date": "2026-11-15",
      "flag": "*",
      "payee": "Example Bookstore",
      "narration": "Books",
      "tags": ["reimbursement"],
      "postings": [
        {"account": "Expenses:Books", "units": "120.00", "currency": "CNY"},
        {"account": "Assets:Bank:Checking", "units": "-120.00", "currency": "CNY"}
      ]
    }
  ],
  "evidence": [
    {"kind": "file", "locator": "documents/2026-11/receipt.pdf", "sha256": "..."}
  ],
  "confidence": 0.91,
  "rationale_summary": "依据收据商户、金额和日期生成。"
}
```

Field rules: amounts must be decimal strings (JSON floats are rejected); every posting must state its amount explicitly; `payee`/`narration` reject newlines, control characters, and bidirectional override characters; accounts/currencies/tags/links follow Beancount syntax constraints; costs, prices, and metadata are outside the v0.1 schema.

Review loop:

```bash
# The Agent submits via MCP or the Python API; the CLI can also call the kernel API directly
ledger-kernel drafts list
ledger-kernel drafts show drf_...
ledger-kernel drafts validate drf_...        # returns validation_id, checks, and diff
ledger-kernel drafts diff drf_... --validation val_...
ledger-kernel drafts approve drf_... --confirm drf_...
ledger-kernel drafts commit drf_...
ledger-kernel corrections propose cmt_...   # produces a reversal draft; still needs validate/approve/commit
```

Approval requires explicit confirmation (the value of `--confirm` must equal the draft ID; for batches, pass the complete comma-separated list). Non-interactive mode never approves just because "the command was called". An approval is bound to `draft_id + validation_id + ledger_revision + contract_revision` and expires after 30 minutes by default.

## MCP (Agent adapter)

```bash
ledger-kernel-mcp --ledger /path/to/ledger
```

stdio transport, one JSON-RPC message per line. Exposes:

- `ledger_get_status`
- `ledger_list_accounts`
- `ledger_get_account`
- `ledger_get_balance`
- `ledger_submit_draft`
- `ledger_get_draft_status`

It **does not** expose approve, commit, write-file, or modify-contract tools. The MCP layer does not reimplement validation or write logic; it only calls `Kernel` use cases.

Client configuration example (Claude Desktop / DSH, etc.):

```json
{
  "mcpServers": {
    "ledger-kernel": {
      "command": "ledger-kernel-mcp",
      "args": ["--ledger", "/path/to/ledger"]
    }
  }
}
```

## Security invariants

1. **Structured proposals**: the Agent cannot submit raw Beancount text; text is rendered by the kernel, which escapes `"` and `\` and rejects control characters.
2. **Full dry-run**: the include closure is copied into a temporary workspace, only `write_target` is replaced, and the whole ledger is loaded with the same Beancount version; non-ledger attachment directories are mirrored via symlinks to avoid false `Document` errors.
3. **ADR-012 delta assertion**: all old entries must still be present; the number of new entries equals the number of proposed transactions and each must match the proposal; `options_map` and the plugin list must be unchanged; other files in the closure must be byte-identical. Any mismatch yields `DELTA_MISMATCH`.
4. **Fragment fingerprint**: validation stores the SHA-256 of the exact bytes to be appended (including any required separator newline); commit re-renders and compares, and also verifies the stored fragment's own hash. A mismatch yields `FRAGMENT_MISMATCH`.
5. **Human approval**: approval is bound to the validation and both revisions; the Agent cannot call approve APIs.
6. **Controlled commit**: single file, append-only; full re-validation before commit, pre-write snapshot, `PREPARED` journal, target hash recheck, final whole-ledger revision recheck, same-directory temp file with `fsync` + `os.replace` + directory `fsync`, and a `COMMITTED` audit event.
7. **Crash recovery**: after restart, decide "applied / not applied / uncertain" from the journal's before/after hashes; only when applied *and* the ledger revision equals the journal record is the commit confirmed, and it is never appended twice; uncertainty leads to `RecoveryRequired` and waits for manual handling.
8. **External changes**: the closure fingerprint is recomputed before every operation; on change the ledger is reloaded, a diff is saved (when an old blob exists), old drafts are invalidated, and `ExternalChangeObserved` is recorded. If an external change makes the ledger invalid, the kernel enters `LedgerInvalid`, blocks controlled commits, but does not modify the user's files.
9. **Known transition**: only when "the current revision equals the old manifest with `write_target` replaced by the after_hash of some COMMITTED journal" is a change considered the kernel's own commit; everything else is an external change. Batch approvals may carry an approval forward while `review_digest` is unchanged (`ApprovalCarriedForward`), without skipping re-validation.
10. **Audit chain**: every event in `events.jsonl` carries `prev_hash` and `event_hash`; a broken or empty chain blocks commits (`AUDIT_INTEGRITY`).

## Design documents

- [Design document index](docs/design/README.md)
- [Design summary](docs/design/design-summary.md)
- [v0.1 design spec](docs/design/v0.1-spec.md)
- [v0.1 engineering decision freeze and prototype implementation plan](docs/design/v0.1-implementation-plan.md)
- [v0.1 implementation record](docs/design/v0.1-implementation-report.md)
- ADRs: [ADR-012 delta assertion](docs/design/adr/ADR-012-delta-assertion.md), [ADR-013 known transition](docs/design/adr/ADR-013-known-transition.md)

These documents describe the v0.1 prototype baseline and are currently available in Chinese only; where they conflict with code behavior, this README, the tests, and the latest ADRs take precedence.

## `.ledger-kernel/` layout

```text
.ledger-kernel/
├── kernel.toml            # binding, write_target, approval policy
├── contracts.yaml         # declarative account contracts (schema_version 1)
├── state.json             # current revision/manifest/state (rebuildable)
├── kernel.lock            # kernel write serialization lock (flock)
├── idempotency.json       # agent + key -> draft_id
├── drafts/<id>.json       # immutable proposal body
├── drafts/<id>.state.json # workflow state, approval records
├── validations/<id>.json  # validation result, fragment, diff, review_digest, manifest
├── audit/events.jsonl     # append-only hash-chained audit
├── journal/<commit>.json  # PREPARED/COMMITTED/ABORTED/RECOVERY_REQUIRED
├── snapshots/blobs/<sha>  # content-addressed snapshots (deduplicated)
├── snapshots/<commit>/    # pre-write copy for that commit
└── observations/<id>.json # external change observations; same-named .diff
```

`state.json` and `snapshots/` can be rebuilt from the ledger, contracts, and audit copies, or discarded; drafts, validations, audit, and journal are workflow records and must not be deleted.

## Account contracts

```yaml
schema_version: 1
accounts:
  Assets:Bank:Checking:
    aliases: [工资卡, 主银行卡]
    policy:
      allowed_agents: [receipt-agent]
      allowed_currencies: [CNY]
      allowed_tags: [reimbursement]
      required_tags: []
      reconciliation:
        cadence: monthly
        hard_deadline: false
      balance_policy:
        mode: warn_only
        min: null
        max: null
```

Hard blocks: account not opened/closed, `allowed_agents`, `allowed_currencies`, `allowed_tags`, `required_tags`.
Warnings only: `reconciliation.cadence`, `balance_policy`, unknown contract accounts, low confidence, missing evidence/mismatched hash.

Unknown fields in the contract file, invalid account names, and duplicate aliases yield `CONTRACT_INVALID` and block controlled commits (fail-closed); reads still work. v0.1 does not support arbitrary Python rules.

## Frozen engineering decisions (implementation plan section 12)

| Item | v0.1 choice |
|---|---|
| Beancount version | 3.2.x (verified on 3.2.3), dependency `>=3.2,<4`; upgrades require real-ledger regression and an engine fingerprint update |
| include closure | `options_map["include"]` from `loader.load_file()`, cross-checked by root file, entry source, and the file system; falls back to entry meta + root with a recorded `closure_source` when missing; includes outside the ledger directory are rejected |
| External snapshots | content-addressed blobs of all files in the closure; default cap 512 MiB; the current manifest, journal, and before/after hashes are protected |
| Commit primitive | same-directory temp file + `fsync` + `os.replace` + directory `fsync`; final revision recheck; no `O_APPEND` (the two primitives do not coexist) |
| Historical error baseline | a clean baseline is required; Beancount errors in the current ledger block validate/commit |
| Contract storage | `.ledger-kernel/contracts.yaml`, `schema_version: 1`, strict fields, declarative |
| MCP transport | stdio, one JSON-RPC message per line; only read and proposal tools are exposed |

## Acceptance test matrix

Run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

| ID | Scenario | Test |
|---|---|---|
| T01 | Load a plain ledger | `test_load_plain_ledger` |
| T02 | Multiple includes are part of the revision | `test_include_files_are_in_revision` |
| T03 | Modify a non-write_target include | `test_non_target_include_change_changes_revision`, `test_external_change_invalidates_validation` |
| T04 | Manual ledger edit while the kernel is closed | `test_external_edit_while_kernel_closed_is_observed_not_overwritten` |
| T05 | Agent submits a valid draft | `test_submit_does_not_touch_ledger` |
| T06 | Idempotent resubmission | `test_idempotency_returns_same_draft` |
| T07 | Violates Beancount semantics | `test_unbalanced_transaction_is_blocked_by_beancount` |
| T08 | Violates a hard account contract | `test_hard_contract_is_blocked` |
| T09 | External change before approval | `test_external_change_invalidates_validation` |
| T10 | External change after approval, before write | `test_external_change_after_approval_blocks_commit` |
| T11 | Two kernels writing simultaneously | `test_second_kernel_cannot_hold_lock` |
| T12 | Crash during commit | `test_crash_after_replace_recovers_without_duplicate`, etc. |
| T13 | Unknown source | `test_external_edit_while_kernel_closed_is_observed_not_overwritten` |
| T14 | External edit makes the ledger invalid | `test_invalid_external_edit_blocks_controlled_commits` |
| T15 | Reverse an existing external transaction | `test_reversal_is_a_new_draft_not_a_delete` (produces a reversal draft, never deletes history) |
| T16 | Delete derived state | `test_deleting_derived_state_rebuilds_from_ledger` |
| T17 | narration injection | `test_newline_injection_is_blocked` |
| T18 | payee quotes/backslashes | `test_quotes_and_backslashes_survive_commit` |
| T19 | Defective renderer adds an extra entry | `test_renderer_defect_adding_an_entry_is_delta_mismatch` |
| T20 | Tamper with the fragment to write | `test_tampered_fragment_is_rejected_at_commit` |
| T21 | Batch approval carry-forward | `test_batch_approvals_carry_forward` |
| T22 | Digest change breaks carry-forward | `test_digest_change_breaks_carry_forward` |
| T23 | External change mid-batch | `test_external_change_mid_batch_invalidates_remaining_approvals` |
| T24 | External append after commit | `test_external_append_after_commit_is_not_a_known_transition` |

The end-to-end check has two layers, both operating only on a temporary copy:

Synthetic fixture (default; CI runs it on every build; covers commit, external change, draft invalidation, and blocking on an invalid ledger):

```bash
PYTHONPATH=src python3 scripts/e2e_ledger.py
```

Real-ledger copy (run locally when writes/recovery/external changes are involved; the `--source` directory is read-only):

```bash
PYTHONPATH=src python3 scripts/e2e_ledger.py --source /path/to/TestAssets
```

## Known limitations (stated honestly for v0.1)

- **Platform**: the current locking and directory `fsync` implementation targets POSIX (`fcntl.flock`); Windows requires a replacement lock implementation.
- **Concurrency**: `flock` only coordinates kernel writes. Ordinary editors do not honor the lock, so a theoretical race window remains between the final hash check and `os.replace`. v0.1 narrows the window and detects drift before/after, but does not promise generic compare-and-swap against uncooperative processes.
- **Attribution**: only the content change is proven; the operator is not. `source_kind` is always `external_unknown` and `attribution_confidence=unknown`; no Agent/user identity is fabricated, and Git/OS audit corroboration is not yet integrated.
- **Plugins**: ledger plugins are a code execution environment trusted by the user. The dry-run executes the same plugin configuration in a temporary directory; plugins that depend on absolute paths or external side effects may not be fully equivalent to a real load. The Agent cannot specify plugins.
- **Attachments**: the dry-run mirrors non-closure top-level directories (such as `document/`) via symlinks to support Beancount `Document` directives; these are read-only views of real files and are never copied or written back.
- **Balances**: balances in diffs are raw posting amounts summed per currency, with no cost/price conversion; full balance assertion semantics are still validated by the official loader.
- **Multi-file transactions**: one commit writes exactly one `write_target`; batch rewrites, history deletion, and cross-file transactions are not supported.
- **Corrections/reversals**: `corrections propose <commit_id>` only produces a reversal draft that still needs validate/approve/commit; the original transaction is never deleted or rewritten. Externally written transactions have no stable `commit_id`, so a one-click reversal draft is not yet available for them.
- **MCP**: minimal stdio implementation (initialize / tools/list / tools/call); no authentication, streaming progress, or resource subscriptions. Remote access requires its own secure channel.
- **Snapshot retention**: default 512 MiB cap; beyond it, the oldest unprotected blobs are evicted. Baselines referenced by the current manifest/journal are never deleted.

## Code structure

```text
src/ledger_kernel/
├── cli.py            # human review CLI
├── mcp_server.py     # MCP stdio adapter (read-only + proposals)
├── kernel.py         # use-case orchestration: state, drafts, approval, commit, recovery
├── adapter.py        # Beancount loading/closure/accounts/balances/normalized entries
├── dryrun.py         # temporary workspace, full validation, ADR-012 delta assertion, diff
├── schema.py         # draft schema, text safety, Beancount rendering
├── contracts.py      # declarative account contracts
├── revision.py       # manifest + ledger/contract revision
├── store.py          # drafts/validations/audit/journal/observations/blobs
├── config.py         # kernel.toml
├── locks.py          # flock
├── errors.py         # stable error codes
└── util.py           # atomic writes, hashes, text, amounts
```

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
PYTHONPATH=src python3 scripts/e2e_ledger.py
PYTHONPATH=src python3 scripts/e2e_ledger.py --source /path/to/TestAssets
```

Tests do not depend on pytest; only `beancount` and `PyYAML` are required. Tests and E2E run in temporary directories and never modify a real ledger; the synthetic fixture lives in `tests/fixtures/ledger`.
