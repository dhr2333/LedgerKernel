English | [中文](README.md)

# LedgerKernel

A local ledger account kernel that wraps a Beancount ledger and provides a closed loop of **proposal → full dry-run → human approval → controlled append → audit → recovery**.

The ledger file is the single source of truth for financial facts; the kernel is the only **controlled entry point for automation writes**. The user keeps the highest authority to edit the ledger directly — the kernel does not block or overwrite edits, it only discovers and records them, and marks changes it cannot safely attribute as `external_unknown`.

> This repository is the v0.1 prototype implementation and, since **v0.2.0 (2026-11-16)**, provides the v0.2 account layer (contract editing, account reconciliation, account-level review). The design baseline — *Design Summary*, *Engineering Decision Freeze and Prototype Implementation Plan*, *Design Spec Draft*, implementation record, v0.2 account-layer design, and ADRs — has exactly one source of truth: [`docs/design/`](docs/design/README.md). External notes keep no copy of it. The implementation covers P0-A to P0-D plus a thin MCP adapter (the minimal tool set of P0-E).

CI: Jenkins (internal, triggered by GitHub webhook; configuration in [ci-github-webhook](docs/ci-github-webhook.en.md)) · Development conventions: [CONTRIBUTING.en.md](CONTRIBUTING.en.md) · Agent conventions: [AGENTS.en.md](AGENTS.en.md) · Release process: [release-checklist](docs/release-checklist.md) · Changelog: [CHANGELOG.en.md](CHANGELOG.en.md)

This document is available in both Chinese and English: `X.md` is Chinese and `X.en.md` is English; both must be updated in the same change (see `AGENTS.md`).

## What this is not

- Not a bookkeeping app, budgeting tool, or spending dashboard.
- Not tied to a specific frontend or product; CLI/MCP/Python API are generic entry points. The goal is a user-owned ledger that can be read, exported, and taken away, not a write backend for one application.
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

Minimal review entry point and draft templates:

```bash
ledger-kernel review                          # review queue: state, accounts, amount, validation warnings
ledger-kernel review drf_... drf_...          # per-entry fragment, checks, and diff
ledger-kernel review drf_... --approve --confirm drf_...
ledger-kernel templates list
ledger-kernel drafts submit --template electricity --amount 99.90 --date 2026-11-15
ledger-kernel drafts submit --payload draft.json
```

```yaml
# .ledger-kernel/templates.yaml
schema_version: 1
templates:
  electricity:
    payee: Electric Utility
    narration: Monthly power
    tags: [Family, Fixed]
    postings:
      - {account: Expenses:Home:Utilities, units: amount}
      - {account: Liabilities:CreditCard:Bank:CMB:C0002, units: -amount}
```

Templates only prefill repeated fields; an expanded draft still needs validate/approve/commit and never bypasses review. Validation reports repeated entries as a `DUPLICATE_SUSPECTED` warning (non-blocking); closed orders and refund pairing remain an import-side decision.

Approval requires explicit confirmation (the value of `--confirm` must equal the draft ID; for batches, pass the complete comma-separated list). Non-interactive mode never approves just because "the command was called". An approval is bound to `draft_id + validation_id + ledger_revision + contract_revision` and expires after 30 minutes by default.

### Minimal review TUI (v0.3, optional external frontend)

`frontends/review-tui/` is a separate installable package (not part of the kernel install; the kernel does not depend on it). It works only through the existing CLI read-only JSON and approval commands:

```bash
python3 -m pip install ./frontends/review-tui
ledger-review-tui --ledger /path/to/ledger          # curses TUI
ledger-review-tui --ledger /path/to/ledger --list   # non-interactive summary
```

Boundaries: no server, no accounts, no network; no automatic or account-level batch approval; approval still requires a human to type the same draft_id confirmation as the CLI. Read-only fields and change rules: [v0.3 frontend JSON contract](docs/design/v0.3-frontend-contract.md); usage: [frontend README](frontends/review-tui/README.md).

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

It **does not** expose approve, commit, write-file, modify-contract, or reconciliation tools. The MCP layer does not reimplement validation or write logic; it only calls `Kernel` use cases.

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
8. **External changes**: the closure fingerprint is recomputed before every operation; on change the ledger is reloaded, a diff is saved (when an old blob exists), old drafts are invalidated, and `ExternalChangeObserved` is recorded. If an external change makes the ledger invalid, the kernel enters `LedgerInvalid`, blocks controlled commits, but does not modify the user's files. When the closure bytes are unchanged the kernel reuses the previous Beancount load (the closure is still re-hashed every time), so external-change detection is never skipped.
9. **Known transition**: only when "the current revision equals the old manifest with `write_target` replaced by the after_hash of some COMMITTED journal" is a change considered the kernel's own commit; everything else is an external change. Batch approvals may carry an approval forward while `review_digest` is unchanged (`ApprovalCarriedForward`), without skipping re-validation.
10. **Audit chain**: every event in `events.jsonl` carries `prev_hash` and `event_hash`; a broken or empty chain blocks commits (`AUDIT_INTEGRITY`).

## Design documents

- [Design document index](docs/design/README.md)
- [Design summary](docs/design/design-summary.md)
- [v0.1 design spec](docs/design/v0.1-spec.md)
- [v0.1 engineering decision freeze and prototype implementation plan](docs/design/v0.1-implementation-plan.md)
- [v0.1 implementation record](docs/design/v0.1-implementation-report.md)
- [v0.2 scope proposal](docs/design/v0.2-scope.md)
- [v0.2 account-layer design](docs/design/v0.2-account-layer.md)
- [v0.2 account-layer implementation plan](docs/design/v0.2-account-layer-plan.md) (implementation results in section 9)
- [v0.3 scope and explicit non-goals](docs/design/v0.3-scope.md) (MVP: review loop; frozen, code not started)
- [v0.3 frontend read-only JSON contract](docs/design/v0.3-frontend-contract.md) (external frontend surface; frozen)
- ADRs: [ADR-012 delta assertion](docs/design/adr/ADR-012-delta-assertion.md), [ADR-013 known transition](docs/design/adr/ADR-013-known-transition.md), [ADR-014 engine fingerprint](docs/design/adr/ADR-014-engine-fingerprint.md), [ADR-015 review digest](docs/design/adr/ADR-015-review-digest.md), [ADR-016 draft templates](docs/design/adr/ADR-016-draft-templates.md), [ADR-017 duplicate warning](docs/design/adr/ADR-017-duplicate-warning.md)
- v0.2 ADRs: [ADR-018 reconciliation is a workflow fact](docs/design/adr/ADR-018-reconciliation-fact.md), [ADR-019 contract apply](docs/design/adr/ADR-019-contract-apply.md), [ADR-020 account-level review](docs/design/adr/ADR-020-account-level-review.md)

These documents describe the v0.1 prototype baseline and the v0.2 account layer (v0.2.0), and are currently available in Chinese only; where they conflict with code behavior, this README, the tests, and the latest ADRs take precedence.

## `.ledger-kernel/` layout

```text
.ledger-kernel/
├── kernel.toml            # binding, write_target, approval policy
├── contracts.yaml         # declarative account contracts (schema_version 1)
├── templates.yaml         # draft templates (schema_version 1)
├── state.json             # current revision/manifest/state plus the reconciliations cache (rebuildable)
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
unmanaged_accounts:
  - Assets:Savings:Cash
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

`unmanaged_accounts` explicitly declares accounts you know exist but do not manage yet: it must not overlap `accounts`, and every name must still be a valid Beancount account. Contract coverage = active accounts with a contract / active accounts; `unmanaged_accounts` is counted separately:

```bash
ledger-kernel accounts list --coverage
```

Unknown fields in the contract file, invalid account names, and duplicate aliases yield `CONTRACT_INVALID` and block controlled commits (fail-closed); reads still work. v0.1 does not support arbitrary Python rules.

### Account registry (v0.4 draft, read-only)

The v0.4 draft promotes account definitions from a read-only projection to a registry: `.ledger-kernel/accounts.yaml` (schema v2) records groups, accounts, aliases and policies, while the ledger stays the append-only factual record. Only the read-only engine is implemented so far; it never writes the ledger:

```bash
ledger-kernel accounts registry            # parsed view, revision, unmanaged accounts, warnings
ledger-kernel accounts plan                # registry vs ledger: missing open/close, alias drift
ledger-kernel accounts resolve 中行工资卡    # alias/id/account name/group path -> one object
```

- A missing file is treated as an empty registry; the schema and example are in [v0.4 draft](docs/design/v0.4-account-first-revision.md), section 4.
- The registry is not the default write path yet: `accounts apply`, `entry` and the account workbench are not implemented and require ADR-021 (delta assertion and digest for account directives).
- Schema v2 is a pre-release draft and may change before it is frozen; the v1 `contracts.yaml` remains the contract file used by the current policy/review flow and is not read as a registry.
- `accounts plan` is read-only: it does not write the ledger, change `ledger_revision`, or append audit events. New error code: `REGISTRY_INVALID`.

### Contract editing (v0.2)

The only write entry point for the contract file is `contracts apply`; no more "edit the YAML and hope":

```bash
ledger-kernel contracts show                                  # parsed view, revision, coverage, errors
ledger-kernel contracts validate --file contracts.new.yaml    # strict parse; never writes
ledger-kernel contracts diff --file contracts.new.yaml        # unified diff and affected accounts
ledger-kernel contracts apply --file contracts.new.yaml \
  --expected-current sha256:<current> --confirm sha256:<new> --note "add utility contract"
```

- `--expected-current` (the revision the caller read; use `absent` when no file exists) and `--confirm` (full SHA-256 of the new file) are both mandatory and must match; missing or mismatched values return `CONTRACT_CONFLICT` and leave the file unchanged.
- Before writing, the candidate is strictly validated: unknown fields, duplicate aliases, invalid account names, invalid cadence/balance ranges, and `unmanaged_accounts` overlap all return `CONTRACT_INVALID` without writing.
- The write uses a same-directory temp file + `fsync` + `os.replace` + directory `fsync` while holding `kernel.lock`; old/new contents go to `snapshots/blobs/` and a `ContractChanged` audit event records both hashes, actor, and note.
- A contract revision change invalidates every pending validation/approval; the next operation revalidates automatically and old approvals are never reused.
- Contract editing never writes the ledger and does not change `ledger_revision`; MCP exposes no such write entry point.

### Account reconciliation (v0.2)

Reconciliation is a **workflow fact**, not a Beancount directive: `accounts reconcile` appends an `AccountReconciled` audit event and `accounts reconciliation revert` appends an `AccountReconciliationReverted` event; neither writes the ledger nor changes `ledger_revision`, and audit history is never deleted or rewritten.

```bash
ledger-kernel accounts reconcile Assets:Bank:Checking --as-of 2026-11-30 \
  --declared CNY=1234.56 --declared USD=100.00 --note "matches the bank app" \
  --confirm Assets:Bank:Checking
ledger-kernel accounts reconciliation list Assets:Bank:Checking
ledger-kernel accounts reconciliation revert rec_... --reason "wrong balance" --confirm rec_...
```

- Record fields: `reconciliation_id`, `account`, `as_of`, `ledger_balances` (end-of-day balances of the account on `as_of`, multi-currency), `declared_balances` (optional declared balances), `differences = declared - ledger` (per declared currency), `declared_not_in_ledger` (declared currencies absent from the ledger balance are computed with ledger=0 and listed here), `ledger_revision`, `contract_revision`, `actor` (`human/local-user`), `recorded_at`, and `note` (optional). Amounts are decimal strings only; a `float` or an invalid amount returns `RECONCILIATION_INVALID`.
- The reconciliation status is derived from the contract `policy.reconciliation.cadence` (`weekly/monthly/quarterly/yearly`) and the latest valid record: `not_configured` / `never` / `current` / `overdue`; `next_due = last.as_of + cadence_days`; `difference_status` is `none/zero/nonzero`. `accounts show` also returns `assertion_status` (`unset`/`asserted`), `last_assertion` (date and amount of the latest Beancount `balance` directive, or `null`), and the `reconciliation` view; a failed assertion is a global `LedgerInvalid` and is never disguised as passing in the account view. Every `accounts list` row carries the same three fields so the whole account layer can be inspected in one read.
- A valid record must be unique per account and `as_of` (revert before recording again); only the latest valid record of an account can be reverted, the status falls back to the previous record, and the history stays in the audit chain.
- Preconditions: the ledger loads without errors, the account is open (not closed) on `as_of`, and `as_of <= today`; otherwise `RECONCILIATION_INVALID` is returned and nothing is written. Reconciliation requires no evidence; `note` is optional.
- **Reconciliation never blocks writes**: the status is only used for hints, sorting, and review views, and `CONTRACT_RECONCILIATION` warns only for `never`/`overdue`; `hard_deadline` is accepted for compatibility but ignored. Declared differences are recorded and shown only: no adjustment entry is generated or committed automatically; adjustments still go through draft → validate → approve → commit.
- `state.json.reconciliations` is a rebuildable cache; when missing or corrupt it is rebuilt from audit events, and reads always take the audit chain as the truth.
- `accounts reconcile` / `accounts reconciliation revert` require `--confirm` to equal the account name / `reconciliation_id`; non-interactive mode never auto-confirms, and MCP exposes no reconciliation write entry point.

### Review by account (v0.2)

Account-level review is a **read-only grouped view**; it introduces no account-level approval object (ADR-020):

```bash
ledger-kernel review --by-account                   # aggregate pending drafts by account
ledger-kernel review --account Assets:Bank:Checking # one account; shown even without pending drafts
ledger-kernel review --by-account --state Approved   # --state still filters first
```

- Grouping rule: by default only accounts with pending drafts (same state set as the `review` queue) appear; a draft appears under every account in its `account_refs`. `--account` shows one group even when that account has no pending drafts.
- Group fields (`groups[]` in `--json` output): `account`, `lifecycle`, `balances`, `reconciliation` (`status` / `next_due` / `difference_status`, including the latest valid record), `contract` (`aliases` / `allowed_agents` / `allowed_currencies` / `allowed_tags` / `required_tags` / `reconciliation` / `balance_policy` when a contract exists, `null` otherwise), `pending_drafts` (`draft_id` / `date` / `amount` / `currency` / `narration` / `state` / `validation_status`), `warnings` (`CONTRACT_*` warnings from the drafts' latest validation, attributed to the account named by the check; checks without an `account` are shown in every group containing the draft; `severity: "warning"` only, never copied as blocks), and `draft_count`.
- Top-level JSON: `mode: "by_account"`, `account` (filter account or `null`), `note` (normally `null`; explains an unknown account), and `groups`. Text output prints one `== account lifecycle balance reconciliation status (next_due)` header per group, followed by a contract summary line, pending lines, and warning lines; an empty group prints `pending: none` and a group without a contract prints `contract: none`.
- Unknown account (absent from the ledger, contracts, and every draft): returns `groups: []` with `note` set to `unknown account: <name>` instead of an error code; text output prints that note.
- **Approval binding is unchanged**: `review --by-account --approve` without an explicit draft_id list still returns `INVALID_STATE`; with explicit draft_ids and `--confirm` it uses the same per-draft v0.1 approval path, still bound to `draft_id + validation_id + ledger_revision + contract_revision`, and the only new audit events are per-draft `DraftApproved`. There is no "approve the whole account" shortcut; without `--approve` the grouped view and explicit draft_id arguments are mutually exclusive (giving both returns `INVALID_STATE`), and with `--approve` approval stays per explicit draft_id and the grouping switches never change approval semantics. The existing `review` queue/per-draft detail and `drafts approve` behavior is unchanged.
- The grouped view is read-only: it never writes the ledger, does not change `ledger_revision`, and adds no audit events or state-machine states; the MCP tool list is unchanged (no review-by-account tool).

## Frozen engineering decisions (implementation plan section 12)

| Item | v0.1 choice |
|---|---|
| Beancount version | 3.2.x (verified on 3.2.3), dependency `>=3.2,<4`; upgrades require real-ledger regression and an engine fingerprint update. The fingerprint contains Beancount and ledger-kernel versions only, not the Python runtime (ADR-014) |
| include closure | `options_map["include"]` from `loader.load_file()`, cross-checked by root file, entry source, and the file system; falls back to entry meta + root with a recorded `closure_source` when missing; includes outside the ledger directory are rejected |
| External snapshots | content-addressed blobs of all files in the closure; default cap 512 MiB; the current manifest, journal, and before/after hashes are protected |
| Commit primitive | same-directory temp file + `fsync` + `os.replace` + directory `fsync`; final revision recheck; no `O_APPEND` (the two primitives do not coexist) |
| Historical error baseline | a clean baseline is required; Beancount errors in the current ledger block validate/commit |
| Contract storage | `.ledger-kernel/contracts.yaml`, `schema_version: 1`, strict fields, declarative |
| MCP transport | stdio, one JSON-RPC message per line; only read and proposal tools are exposed |

### v0.2 account-layer frozen decisions (implementation plan section 0)

| Item | Choice |
|---|---|
| Contract editing | The only write entry point is `contracts apply`: `--expected-current` + `--confirm` dual-hash CAS, atomic replace, `ContractChanged` audit with before/after blobs; on failure the file is unchanged (ADR-019) |
| Account reconciliation | Append-only audit events `AccountReconciled` / `AccountReconciliationReverted`; status derived from `cadence` and dates, no evidence required, never blocks writes; `hard_deadline` is accepted but ignored (ADR-018) |
| Declared balances | Optional `--declared CUR=VALUE`; records `ledger_balances / declared_balances / differences`; differences are read-only and never create adjustments or commits |
| Account-level review | `review --by-account/--account` read-only grouped view; no account-level approval object, approval still binds `draft_id + validation_id + ledger_revision + contract_revision` (ADR-020) |

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

### v0.2 account-layer acceptance

| ID | Scenario | Test |
|---|---|---|
| A01 | Contract apply: replace file, `ContractChanged` audit, before/after blobs, `ledger_revision` unchanged | `test_apply_replaces_file_records_audit_and_keeps_ledger_revision` |
| A02 | Contract CAS conflict / confirm mismatch / missing hash → `CONTRACT_CONFLICT`, file unchanged | `test_apply_rejects_stale_expected_current_without_writing`, `test_apply_requires_expected_current`, `test_apply_requires_matching_confirm_hash` |
| A03 | Invalid schema or `unmanaged_accounts` overlap → `CONTRACT_INVALID`, file unchanged | `test_apply_rejects_invalid_contracts_without_writing` |
| A04 | A contract change invalidates pending validation/approval; revalidation then approval | `test_apply_invalidates_pending_validation_and_approval` |
| A05 | Contract coverage and `unmanaged_accounts` counting | `test_contracts_view_warns_about_unopened_accounts`, `test_accounts_list_coverage_reports_managed_and_unmanaged` |
| A06 | Reconciliation record succeeds, audit event recorded, `ledger_revision` unchanged | `test_record_success_persists_record_and_audit_event` |
| A07 | Reconciliation status `current/overdue/never/not_configured` and `next_due` | `test_current_status_and_next_due`, `test_overdue_status_and_next_due`, `test_never_and_not_configured_statuses` |
| A08 | Declared differences: undeclared/zero/nonzero/multi-currency/currency absent from ledger | `test_zero_difference_is_recorded`, `test_nonzero_difference_is_recorded`, `test_multi_currency_balances_and_differences`, `test_declared_currency_absent_from_ledger_uses_zero` |
| A09 | Invalid ledger / account not open / future `as_of` / duplicate `as_of` → `RECONCILIATION_INVALID`, nothing written | `test_invalid_ledger_is_rejected`, `test_account_not_open_on_as_of_is_rejected`, `test_future_as_of_is_rejected`, `test_duplicate_as_of_is_rejected` |
| A10 | Only the latest record can be reverted; status rolls back and history is kept | `test_revert_requires_the_latest_record`, `test_revert_restores_previous_status_and_keeps_history` |
| A11 | Missing/corrupt cache rebuilds from audit; reads trust the audit chain | `test_missing_state_json_rebuilds_cache_from_audit`, `test_corrupt_state_cache_rebuilds_from_audit`, `test_reads_derive_truth_from_audit_not_from_cache` |
| A12 | Overdue reconciliation still never blocks commits | `test_overdue_warning_still_does_not_block_commit` |
| A13 | `review --by-account` grouping and `--account` filtering | `test_by_account_groups_each_draft_under_its_accounts`, `test_account_filter_limits_to_one_group` |
| A14 | The grouped view does not change approval binding | `test_by_account_approve_requires_explicit_ids_and_binds_each_draft` |

The end-to-end check has two layers, both operating only on a temporary copy:

Synthetic fixture (default; CI runs it on every build; covers commit, external change, draft invalidation, and blocking on an invalid ledger):

```bash
PYTHONPATH=src python3 scripts/e2e_ledger.py
```

Real-ledger copy (run locally when writes/recovery/external changes are involved; the `--source` directory is read-only):

```bash
PYTHONPATH=src python3 scripts/e2e_ledger.py --source /path/to/TestAssets
```

## Known limitations (stated honestly)

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
- **Account-layer metadata**: the contract apply dual-hash CAS only protects against concurrent kernels and mistaken writes; a plain editor can still create a theoretical race between the hash check and `os.replace`. Reconciliation status is a date-driven workflow hint that does not automatically expire after later commits; declared differences are read-only and never generate adjustment entries.
- **Review by account**: `review --by-account` is a read-only grouped view and does not support account-level batch approval; account-level shortcuts are deferred to a v0.3 evaluation (ADR-020).
- **Performance**: reads and metadata operations reuse the loaded result when the closure bytes are unchanged; the first load and the full `validate` dry-run remain the main costs (real-ledger copy with 134 files: about 0.7 s for the first load and about 1.1 s per `validate`). Audit append is still O(n) and needs an index at large event counts (see the [dogfood round report](docs/design/v0.2-dogfood-round2.md)).

## Code structure

```text
src/ledger_kernel/
├── cli.py            # human review, contract-editing and reconciliation CLI
├── mcp_server.py     # MCP stdio adapter (read-only + proposals)
├── kernel.py         # use-case orchestration: state, drafts, approval, commit, recovery, contracts and reconciliation
├── adapter.py        # Beancount loading/closure/accounts/balances/normalized entries
├── dryrun.py         # temporary workspace, full validation, ADR-012 delta assertion, diff
├── schema.py         # draft schema, text safety, Beancount rendering
├── contracts.py      # declarative account contracts, parsing and coverage
├── revision.py       # manifest + ledger/contract revision
├── store.py          # drafts/validations/audit/journal/observations/blobs
├── config.py         # kernel.toml
├── locks.py          # flock
├── errors.py         # stable error codes
└── util.py           # atomic writes, hashes, text, amounts

frontends/
└── review-tui/       # optional external review TUI (v0.3, separate install; the kernel does not depend on it)
```

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
PYTHONPATH=src python3 scripts/e2e_ledger.py
PYTHONPATH=src python3 scripts/e2e_ledger.py --source /path/to/TestAssets

# optional external review frontend (v0.3)
cd frontends/review-tui && PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

Tests do not depend on pytest; only `beancount` and `PyYAML` are required. Tests and E2E run in temporary directories and never modify a real ledger; the synthetic fixture lives in `tests/fixtures/ledger`.
