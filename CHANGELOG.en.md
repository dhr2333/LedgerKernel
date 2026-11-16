English | [中文](CHANGELOG.md)

# Changelog

All notable changes are recorded here. The format follows Keep a Changelog; versions follow Semantic Versioning.

## [Unreleased]

### Added

- Synthetic fixture ledger `tests/fixtures/ledger`: nested includes, a plugin, pad/balance assertions, a closed account, and a fixed `write_target`; no real data.
- Real-ledger dogfood protocol `docs/dogfood.en.md`: sample, metrics, and v0.2 prioritisation rules.
- v0.2 scope proposal `docs/design/v0.2-scope.md`: five metrics, P0 defects, and candidate ranking from the 4-week real-ledger dogfood (frozen 2026-11-16; P0/P1 implemented).
- Minimal review entry point `ledger-kernel review`: pending queue, per-draft fragment/checks, and `--approve --confirm` batch approval.
- Draft templates and submission entry point: `.ledger-kernel/templates.yaml`, `ledger-kernel templates list|show`, `ledger-kernel drafts submit --template|--payload` (ADR-016).
- Advisory `DUPLICATE_SUSPECTED` warning at validation: repeated ledger entries are visible but not blocked (ADR-017).
- ADR-014 (engine fingerprint excludes Python), ADR-015 (review digest covers transaction content), ADR-016 (draft templates), ADR-017 (duplicate warning).
- The end-to-end script `scripts/e2e_ledger.py` covers commit, external-change observation with diff, draft invalidation, and blocking commits on an invalid ledger; CI runs it on every build and a `unittest` integration case wraps it.

### Changed

- `engine_fingerprint` now contains Beancount and ledger-kernel versions only, not the Python runtime (ADR-014): a reviewer on another interpreter no longer invalidates every validation.
- `review_digest` now hashes the rendered transactions instead of the append fragment (ADR-015): after the first commit to an empty `write_target`, the remaining approved drafts keep their approvals.
- v0.2 scope revision 1: back to "the account is the product core"; contract editing, account state/reconciliation, and account-level review move to P1, while import reconciliation, batch performance, and metadata become supporting items.
- Renamed `scripts/e2e_testassets.py` to `scripts/e2e_ledger.py`: it defaults to the synthetic fixture and still accepts `--source <TestAssets>` for a real-ledger copy.
- CI triggering is now spelled out as GitHub webhook → internal Jenkins Multibranch; the result is written back as a commit status (context `continuous-integration/jenkins/branch`). Configuration steps: `docs/ci-github-webhook.en.md`.
- Added a maintainer-local Jenkins token rotation convention (`jk-token rotate`, fail-closed, verify before revoke); credentials never enter the repository.
- Documentation is now bilingual: `README`, `CONTRIBUTING`, `CHANGELOG`, and `AGENTS` each have an English counterpart (`*.en.md`). The Chinese file is the source text. No code behavior changes.

### Fixed

- A reviewer running a different Python version triggered `EngineChanged`, invalidated all 9 drafts, and approval failed silently.
- After the first commit to an empty `write_target`, the remaining drafts' `review_digest` changed and batch approval honored only the first entry.

## [0.1.0] - 2026-11-15

### Added

- Beancount Adapter: official `loader.load_file()`, include closure, account tree/balances, revision manifest, engine fingerprint.
- Revision Observer: external change detection, content-addressed snapshots, diffs, `ExternalChangeObserved`, invalidation of old drafts, blocking on an invalid ledger.
- Structured drafts: immutable JSON schema, idempotency, evidence references, text safety rules.
- Full dry-run: whole-ledger load in a temporary workspace, ADR-012 delta assertion, options/plugin/other-file consistency checks, balance and file diffs, review digest.
- Declarative account contracts: hard-block and warning tiers, strict validation of unknown fields.
- Human approval + controlled commit: single `write_target` append, pre-write snapshot, journal, secondary validation, final revision recheck, atomic replace, crash recovery.
- Hash-chained audit, external change observations, `ApprovalCarriedForward`, `corrections propose` reversal drafts.
- CLI review loop and a read-only/proposal MCP stdio adapter.
- 55 unit/integration tests and a TestAssets real-ledger copy E2E script.

#### Compatibility record

- Python ≥3.11; Beancount `>=3.2,<4` (validated on 3.2.3).
- `engine_fingerprint` = `beancount-<version>;python-<version>;ledger-kernel-<version>`; any of the three changes it, and it is part of every ledger revision.
- Draft schema `1`, contract schema `1`; `.ledger-kernel` state/journal/validation/observation records are all `schema_version: 1`.
- First release: no older drafts/validations/journals exist, so no migration is required.

### Known limitations

- Single-file append and a single active write target only; no multi-file transactions or multi-device multi-master writes.
- POSIX `flock`; Windows requires a replacement lock implementation.
- External source is always `external_unknown`; Git/OS audit corroboration is not integrated.
- Balance diffs do not convert costs/prices; full semantics are validated by Beancount.
