English | [中文](README.md)

# LedgerKernel review TUI (ledger-kernel-review-tui)

The external review frontend skeleton for LedgerKernel v0.3: a minimal standard-library curses TUI that wires the kernel's existing read-only review views to a per-draft human approval flow. It is a **standalone install unit inside the same repository but outside the kernel**; the kernel package neither depends on it nor installs it by default.

## Purpose

- Browse pending drafts grouped by account (from the read-only `ledger-kernel review --by-account --json` view).
- Inspect the selected draft: the fragment to append, validation checks and a diff summary.
- Run `ledger-kernel drafts validate` for the selected draft, then require the human to type the same confirmation string as the CLI before calling `ledger-kernel drafts approve --confirm`.
- Provide a non-interactive `--list` summary for SSH, scripts or environments without curses.
- Do not modify kernel code and do not add a write path.

## Boundaries

- **Read-only plus existing CLI writes**: reads use only `review --json` and `review <draft_id> --json`; writes use only the existing `drafts validate` and `drafts approve --confirm`.
- **No kernel import**: the sources contain no `ledger_kernel` import (a boundary test scans for it); the frontend only consumes the kernel CLI JSON contract.
- **No server, no network listener, no accounts**: local single user; no new network I/O.
- **No automatic approval, no skipped confirmation, no account-level bulk approval**: exactly one selected draft is approved per action; the draft id must be typed by hand in the TUI, and a mismatch cancels without calling the kernel.
- **No duplicated kernel validation logic**: `validate` is executed entirely by `ledger-kernel`; the frontend only displays the result.
- The approval binding (`draft_id + validation_id + ledger_revision + contract_revision`) remains enforced by the kernel; the frontend is not part of it.
- POSIX-only (standard-library curses); Windows is out of scope for v0.3 (see `docs/design/v0.3-scope.md` D05).

## Requirements and installation

- Python 3.11+
- An installed and initialized `ledger-kernel` CLI (v0.2.0; this frontend depends only on its review JSON fields)
- No third-party runtime dependencies

Install from the repository root (standalone package, does not affect the kernel package):

```bash
python3 -m pip install ./frontends/review-tui
# development mode
python3 -m pip install -e ./frontends/review-tui
```

Or run from source without installing:

```bash
PYTHONPATH=frontends/review-tui/src python3 -m ledger_review_tui --ledger /path/to/ledger
```

## Running

```bash
# default: curses TUI (the ledger directory must already have .ledger-kernel/ state)
ledger-review-tui --ledger /path/to/ledger
ledger-review-tui --ledger /path/to/ledger --kernel-cmd ledger-kernel

# non-interactive: print the grouped pending queue and exit
ledger-review-tui --ledger /path/to/ledger --list
```

Example `--list` output (format illustration, not real ledger data):

```text
== Assets:Bank  open  1000.00 CNY  reconciliation current
   drf_abc123  2026-11-15  120.00 CNY  Books  [NeedsHumanApproval / pass]
   warning: CONTRACT_CURRENCY  currency not allowed  [drf_abc123]
== Expenses:Books  open  -120.00 CNY  reconciliation not_configured
   drf_def456  2026-11-16  35.00 CNY  Lunch  [Submitted / unvalidated]
```

## TUI keys and approval flow

| Key | Action |
| --- | --- |
| `Up` / `Down` (or `k` / `j`) | Move between account groups and draft rows |
| `Enter` | Show the selected draft detail (fragment / checks / diff, read-only pager) |
| `v` | Run `drafts validate` for the selected draft and show the result |
| `a` | Approve the selected draft (see below) |
| `r` | Re-read `review --by-account` |
| `q` | Quit |

Approval flow (per draft):

1. Put the cursor on a draft row (not an account group row) and press `a`.
2. The footer asks `type exactly 'drf_...' to approve`.
3. Type the draft id manually and press Enter.
4. Only an exact match with the CLI confirmation string (the draft id for a single draft) proceeds; a mismatch cancels immediately without calling the kernel.
5. The frontend calls `drafts approve <draft_id> --confirm <draft_id>` and refreshes the list on success.

The bottom status line shows the latest action or kernel error; failures are never retried automatically.

## Development and tests

Standard-library `unittest`; tests start no real kernel and need no interactive terminal:

```bash
PYTHONPATH=frontends/review-tui/src python3 -m unittest discover -s frontends/review-tui/tests -t frontends/review-tui
```

Covers: command construction and JSON parsing (FakeRunner), CLI-error-to-exception mapping, approval requiring an explicit and exact confirm (the kernel is not called on mismatch), grouping/selection/confirmation-string/detail rendering, `--list` output (injected fake data, no subprocess), and a boundary scan proving the sources do not import `ledger_kernel`.

## Known limitations

- One selected draft per approval; no multi-select and no account-level one-click approval (this is a safety boundary, not a to-do).
- No reject / withdraw / commit / scan / contract-edit / reconciliation entry points; keep using the CLI for those.
- No cache and no file watching; press `r` to refresh. External changes are detected by the kernel on each call.
- No color theme, mouse or search/filter; tiny terminals show a hint instead of crashing.
- Depends on the kernel CLI read-only JSON contract; field changes must be mirrored in this package.
- POSIX-only (curses); Windows is out of scope for v0.3.
- Skeleton only: no PyPI release, no independent version strategy, and CI does not yet pin a `ledger-kernel` version range.

## Related documents

- Scope and non-goals: `docs/design/v0.3-scope.md`
- Kernel review flow and JSON surface: repository root `README.md`
- Security invariants: repository root `README.md` and `AGENTS.md`
