English | [中文](dogfood.md)

# Real-ledger dogfood log (v0.2.x account-layer round)

This document defines the real-ledger usage protocol for v0.2.x. The point is not to re-run the unit tests, but to answer two questions:

1. On real accounts, is the kernel loop faster, less error-prone, and more auditable than manual bookkeeping?
2. Does the v0.2 account layer (contract editing, account reconciliation, account-level review) actually reduce round trips, instead of degrading into an account list?

The answer must come from actual usage data, not a feature wishlist. Without this data, do not rank v0.3 features.

## Safety boundaries

- Use a **copy** of the ledger; keep the original directory read-only and do not modify it during dogfood.
- Take a full backup first and record the copy's `ledger_revision` (`ledger-kernel status`).
- File defects as issues and fix them through the normal process; do not change kernel behavior ad hoc during dogfood.
- Never commit a real ledger, `.ledger-kernel/`, evidence files, or personal data.

## Sample and duration

- 2–4 weeks, at least 20 real transactions.
- Cover four kinds: everyday expenses, income, transfers between accounts, and reversals/corrections of a wrong transaction.
- Every entry goes through the full loop: submit draft → `validate` → `approve` → `commit`; never skip the dry-run and never take batch shortcuts.
- Account-layer requirements (new in this round):
  - At least 2 asset/liability accounts have contracts; at least one monthly cadence.
  - Complete at least one reconciliation during the round (with optional declared balances) and explain any difference (never auto-adjust).
  - Use `review --by-account` for at least one real review.
  - Account-layer operations write only `.ledger-kernel/`, never the ledger; never fake a reconciliation to make status look good.

## Per-entry record (write loop)

| Date | draft_id | submit→approve time | dry-run result | block/warning code | manual file edit | observation/recovery | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
|  |  | seconds | pass / blocked |  | yes / no |  |  |

"Manual file edit" means the entry was only completed after editing the ledger directly, bypassing the kernel. This is the friction point v0.2 most needs to know about.

## Per-round account-layer record

| Metric | Definition | Value |
| --- | --- | --- |
| Contract coverage | active accounts with a contract / active accounts (`unmanaged` counted separately) |  |
| Reconciliation timeliness | accounts reconciled within cadence / accounts with a cadence |  |
| Stale accounts | accounts past their cadence without a reconciliation |  |
| Declared differences | undeclared / zero / nonzero; whether each difference is explainable |  |
| Account-level review time | wall-clock time for one real `review --by-account` review |  |
| Contract/reconciliation friction | manual `contracts.yaml` edits, manual hash copying, CLI bypasses |  |

## Weekly roll-up

Run inside the ledger copy (global flags go before the subcommand):

```bash
ledger-kernel --json status
ledger-kernel drafts list
ledger-kernel audit --limit 100
ledger-kernel observations list
ledger-kernel recovery status
ledger-kernel contracts show
ledger-kernel --json accounts list --coverage
ledger-kernel accounts reconciliation list Assets:Bank:Checking
ledger-kernel review --by-account
```

Record: draft state distribution, audit event count, observation count, recovery status, average approval time, and the account-layer metrics above.

## From data to decisions (apply in order; the first hit reorders priorities)

1. **Any recovery, audit-integrity failure, or duplicate commit**: stop new features; fix it and add an ADR plus regression tests first.
2. **Median submit→approve time > 60 seconds, or the same fields filled repeatedly**: prioritise a minimal review front end and draft templates, not attribution.
3. **Dry-run blocks ≥ 30% and they cluster around accounts/contracts**: prioritise contract editing and account hints.
4. **At least 3 unexplainable observations**: only then consider Git/OS audit attribution; otherwise downgrade it.
5. **Manual file edits ≥ 20%**: improve pre-validation, evidence capture, or draft generation; do not introduce automatic approval tiers.
6. **Contract coverage < 80%, or no growth across the round**: prioritise contract editing/coverage workflows (including explicit `unmanaged` declarations); never auto-fix.
7. **Reconciliation timeliness < 80% for accounts with a cadence, or stale > 0**: prioritise reconciliation overview and hints (read-only aggregation); still never block writes.
8. **`review --by-account` review time is not lower than per-entry review**: re-evaluate account-level interaction; any account-level batch approval needs a new ADR.
9. **Declared differences cannot be explained or are repeatedly mis-entered**: improve declared input and difference display; never auto-generate adjustment entries.

## Where the conclusion goes

- Write the roll-up into a round report under this repository's `docs/design/` (this round: `v0.2-dogfood-round2.md`); notes outside the repository are process records only, never the specification.
- The conclusion must include the sample size and duration, actual values for the five write-loop metrics and the account-layer metrics, the friction points and fixes, and the resulting ranking recommendation for v0.3 candidates.
