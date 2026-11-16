English | [中文](AGENTS.md)

# LedgerKernel In-Repository Agent Conventions

This file constrains Agents working inside this repository. It only states rules that change behavior or security boundaries; background and design are in `README.md`.

## Read before starting

1. `README.md`: boundaries, interfaces, known limitations.
2. `CONTRIBUTING.md`: development, testing, documentation, and commit conventions.
3. Read `docs/release-checklist.md` when a release is involved.

## Hard constraints (non-bypassable)

1. **Real ledgers are read-only**: tests, demos, and development must not modify real ledgers or the `TestAssets` original directory; always use a temporary copy.
2. **Single controlled write entry point**: automation writes in production code may only go through `Kernel`; never append to or rewrite ledger files directly in the CLI, MCP, or scripts.
3. **Human approval**: never add automatic approval, Agent-on-behalf approval, or "skip approval" code paths. Approval must be bound to `draft_id + validation_id + ledger_revision + contract_revision`.
4. **Audit and journal are undeletable and unrewritable**: never clear the audit chain, journal, or observations just to "make tests pass"; recovery only goes through the `recover` state machine.
5. **Beancount version is pinned**: currently supports `>=3.2,<4` (verified on 3.2.3). Upgrades require a real-ledger regression run and updates to the `engine_fingerprint`-related tests.
6. **Never commit secrets or real data**: no real ledgers, `.ledger-kernel/`, API tokens, PATs, private keys, or evidence files containing personal information.
7. **Do not weaken security invariants**: ADR-012 delta assertion, fragment fingerprints, observe-but-never-overwrite for external changes, and fail-closed behavior must not be removed for convenience.

## Credential use

- Operational credentials such as CI / Jenkins tokens are stored and rotated locally by the maintainer (see section 4 of `docs/ci-github-webhook.md`); they never enter the repository, commit messages, build logs, or example configuration.
- When an Agent needs a credential, it reads it from the fixed location the maintainer designates; the maintainer rotates it afterwards per that document. Never copy a token into any repository file or temporary script.
- Use least-privilege accounts only; administrator credentials are reserved for the operational actions listed in the documentation.

## Documentation update rules

Triggered by "did behavior change?", not required on every change:

- Public API, CLI, configuration, schema, or installation changes → update `README.md` / `README.en.md` and the related docstrings.
- Security invariant, irreversible decision, or compatibility boundary changes → add/revise an ADR, which belongs only in this repository's `docs/design/adr/`.
- Internal refactors, extra tests, comment fixes only → no README update required.
- Release-related → update `CHANGELOG.md` / `CHANGELOG.en.md` and follow `docs/release-checklist.md`.

### Design document authority

- Normative design (design summary, design spec, implementation plan, ADRs, implementation report) has exactly one source of truth: this repository's `docs/design/`.
- External notes (the Obsidian design library) never carry a copy of normative text; they hold project status and unfrozen discussion drafts only. Drafts marked as such must land in the repository before anything relies on them.
- When two copies disagree, the one that tests or a commit can verify wins — that is this repository.

### Bilingual synchronization

- User-facing documents exist in two versions: `X.md` (Chinese) and `X.en.md` (English). This covers `README`, `CONTRIBUTING`, `CHANGELOG`, and `AGENTS`.
- Any content change must be applied to both versions in the same change. "Translated later" is not an acceptable state for a merged change.
- The Chinese version is canonical for wording; the English version must convey the same rules, constraints, and numbers. Never let the two diverge in meaning.
- Keep identifiers in examples (account names, commands, IDs, config keys) byte-identical across versions so readers can copy either one.
- When adding a new user-facing document, add both files and its language switcher line on the first line of each.
- An Agent's own reading preference (e.g. only reads Chinese) does not exempt it from updating the English version.

## Definition of Done

- New/modified behavior has corresponding tests; tests still use the standard-library `unittest`.
- Passes locally: `PYTHONPATH=src python3 -m unittest discover -s tests -t .`
- Synthetic-fixture end-to-end loop: `PYTHONPATH=src python3 scripts/e2e_ledger.py` (CI runs it on every build; it covers commit, external change, draft invalidation, and blocking on an invalid ledger).
- Changes involving writes, recovery, or external changes additionally run against a copy of a real ledger: `PYTHONPATH=src python3 scripts/e2e_ledger.py --source <TestAssets>` (explain why it is skipped when no TestAssets is available; the source directory is read-only and the script only operates on a temporary copy).
- Public behavior changes have updated documentation in both languages; decision changes are recorded in an ADR.
- `git status` contains only files relevant to this change; do not tidy unrelated content along the way.
- Do not commit `.venv/`, `__pycache__/`, build artifacts, or temporary ledgers.

## Code conventions

- Python 3.11+; the domain layer does not depend on CLI/MCP/HTTP.
- Use `Decimal` or decimal strings for amounts; never let JSON floats enter ledger semantics.
- Use stable codes from `ledger_kernel.errors`; do not substitute a bare `Exception`.
- Keep the v0.1 boundary of single-file append and a single `write_target`; write an ADR before extending it.
