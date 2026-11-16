English | [中文](dogfood.md)

# Real-ledger dogfood log (input for v0.2 decisions)

This document defines the real-ledger usage protocol between v0.1 and freezing the v0.2 scope. The point is not to re-run the unit tests, but to answer one question:

> On real accounts, is the kernel loop faster, less error-prone, and more auditable than manual bookkeeping?

The answer must come from actual usage data, not a feature wishlist. Without this data, do not rank v0.2 features.

## Safety boundaries

- Use a **copy** of the ledger; keep the original directory read-only and do not modify it during dogfood.
- Take a full backup first and record the copy's `ledger_revision` (`ledger-kernel status`).
- File defects as issues and fix them through the normal process; do not change kernel behavior ad hoc during dogfood.
- Never commit a real ledger, `.ledger-kernel/`, evidence files, or personal data.

## Sample and duration

- 2–4 weeks, at least 20 real transactions.
- Cover four kinds: everyday expenses, income, transfers between accounts, and reversals/corrections of a wrong transaction.
- Every entry goes through the full loop: submit draft → `validate` → `approve` → `commit`; never skip the dry-run and never take batch shortcuts.

## Per-entry record

| Date | draft_id | submit→approve time | dry-run result | block/warning code | manual file edit | observation/recovery | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
|  |  | seconds | pass / blocked |  | yes / no |  |  |

"Manual file edit" means the entry was only completed after editing the ledger directly, bypassing the kernel. This is the friction point v0.2 most needs to know about.

## Weekly roll-up

Run inside the ledger copy (global flags go before the subcommand):

```bash
ledger-kernel --json status
ledger-kernel drafts list
ledger-kernel audit --limit 100
ledger-kernel observations list
ledger-kernel recovery status
```

Record: draft state distribution, audit event count, observation count, recovery status, and the average approval time.

## From data to v0.2

Apply these in order; the first hit reorders the priorities:

1. **Any recovery, audit-integrity failure, or duplicate commit**: stop new features; fix it and add an ADR plus regression tests first.
2. **Median submit→approve time > 60 seconds, or the same fields filled repeatedly**: prioritise a minimal review front end and draft templates, not attribution.
3. **Dry-run blocks ≥ 30% and they cluster around accounts/contracts**: prioritise contract editing and account hints.
4. **At least 3 unexplainable observations**: only then consider Git/OS audit attribution; otherwise downgrade it.
5. **Manual file edits ≥ 20%**: improve pre-validation, evidence capture, or draft generation; do not introduce automatic approval tiers.

## Where the conclusion goes

- Write the roll-up as a v0.2 scope document or ADR under this repository's `docs/design/`; notes outside the repository are process records only, never the specification.
- The conclusion must include the sample size, the actual values of the five metrics above, and which candidate features were rejected because of them.
