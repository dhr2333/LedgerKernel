#!/usr/bin/env python3
"""End-to-end check of the controlled write loop on a disposable ledger copy.

By default this runs against the synthetic fixture in ``tests/fixtures/ledger``
so that anybody can reproduce the loop (and CI can run it on every build) with
no private data:

    PYTHONPATH=src python3 scripts/e2e_ledger.py

To validate a real ledger, point ``--source`` at a directory holding the root
``main.bean``. Everything happens in a temporary copy; the source is never
modified:

    PYTHONPATH=src python3 scripts/e2e_ledger.py --source <TestAssets>

Covered steps:

1. init + first load (closure, accounts);
2. draft idempotency, validate, approve, commit;
3. reload shows exactly one committed transaction and no observation;
4. external edit of a non-target file is detected, diff captured, source unknown;
5. a draft validated before an external edit becomes stale and cannot be approved;
6. an external edit that invalidates the ledger blocks controlled commits;
7. recovery journal is empty after all of the above.

Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

from beancount import loader
from beancount.core import data

from ledger_kernel.errors import KernelError
from ledger_kernel.kernel import Kernel

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "ledger"

TARGET = "transactions/kernel-import.bean"
TARGET_HEADER = "; LedgerKernel write_target\n"

# The fixture is small and known; fixed accounts keep the check deterministic.
FIXTURE_ASSET = "Assets:Bank:Checking"
FIXTURE_EXPENSE = "Expenses:Food:Groceries"
FIXTURE_DATE = dt.date(2026, 3, 1)
# A non-target file inside the fixture include closure.
FIXTURE_EXTERNAL_FILE = "transactions/2026-02.bean"


def choose_accounts(root: Path) -> tuple[str, str, dt.date]:
    """Pick an unasserted, still-open asset/expense pair from a real ledger."""
    entries, errors, _ = loader.load_file(str(root))
    if errors:
        raise SystemExit(f"source ledger has {len(errors)} errors; E2E requires a clean baseline")
    asserted = {entry.account for entry in entries if isinstance(entry, data.Balance)}
    opened = {entry.account for entry in entries if isinstance(entry, data.Open)}
    closed = {entry.account for entry in entries if isinstance(entry, data.Close)}
    dates = [entry.date for entry in entries]

    usage: Counter = Counter()
    for entry in entries:
        if isinstance(entry, data.Transaction):
            for posting in entry.postings:
                usage[posting.account] += 1

    def candidates(prefix: str) -> list[str]:
        return sorted(
            (
                account
                for account in opened - closed - asserted
                if account.startswith(prefix) and ":" in account
            ),
            key=lambda account: (-usage[account], account),
        )

    expenses = candidates("Expenses:")
    assets = candidates("Assets:")
    if not expenses or not assets:
        raise SystemExit("could not find an unasserted expense/asset account pair")
    return assets[0], expenses[0], max(dates) + dt.timedelta(days=1)


def append_block(path: Path, block: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "\n" + block.strip("\n") + "\n", encoding="utf-8")


def external_transaction(day: dt.date, asset: str, expense: str, narration: str) -> str:
    return (
        f"{day.isoformat()} * \"External Editor\" \"{narration}\"\n"
        f"  {expense}    7.00 CNY\n"
        f"  {asset}     -7.00 CNY\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="ledger directory containing main.bean (default: synthetic fixture)",
    )
    parser.add_argument("--asset", help="asset account used by the draft")
    parser.add_argument("--expense", help="expense account used by the draft")
    parser.add_argument("--keep", action="store_true", help="keep the temporary ledger copy")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args()

    source = (args.source or FIXTURE).expanduser().resolve()
    using_fixture = source == FIXTURE.resolve()
    if not (source / "main.bean").is_file():
        raise SystemExit(f"no main.bean under {source}")

    workdir = Path(tempfile.mkdtemp(prefix="ledger-kernel-e2e-"))
    ledger = workdir / "ledger"
    shutil.copytree(
        source,
        ledger,
        ignore=shutil.ignore_patterns(
            ".git", ".ledger-kernel", ".stversions", ".stfolder", "__pycache__", "*.pyc"
        ),
    )

    target_path = ledger / TARGET
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if not target_path.exists():
        target_path.write_text(TARGET_HEADER, encoding="utf-8")
    main_bean = ledger / "main.bean"
    main_text = main_bean.read_text(encoding="utf-8")
    if f'include "{TARGET}"' not in main_text:
        main_bean.write_text(main_text.rstrip("\n") + f'\n\ninclude "{TARGET}"\n', encoding="utf-8")

    if using_fixture:
        asset, expense, day = FIXTURE_ASSET, FIXTURE_EXPENSE, FIXTURE_DATE
        external_file = ledger / FIXTURE_EXTERNAL_FILE
    else:
        asset, expense, day = choose_accounts(main_bean)
        external_file = main_bean
    if args.asset:
        asset = args.asset
    if args.expense:
        expense = args.expense

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: object = None) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    report: dict = {
        "source": str(source),
        "fixture": using_fixture,
        "ledger": str(ledger),
        "accounts": [asset, expense],
        "date": day.isoformat(),
    }

    kernel = Kernel.init(main_bean, TARGET)
    status = kernel.get_status()
    report["init"] = {
        "status": status["status"],
        "closure_files": status["files_in_closure"],
        "accounts": len(kernel.list_accounts()),
        "target_in_closure": status["write_target_in_closure"],
    }
    check("init clean", status["status"] == "Clean", status["status"])
    check("closure includes include graph", status["files_in_closure"] >= 6, status["files_in_closure"])
    check("write_target in closure", status["write_target_in_closure"])
    check("accounts discovered", len(kernel.list_accounts()) >= 8, len(kernel.list_accounts()))

    body = {
        "schema_version": 1,
        "operation": "append_transactions",
        "transactions": [
            {
                "date": day.isoformat(),
                "flag": "*",
                "payee": "E2E Shop",
                "narration": "LedgerKernel end-to-end check",
                "postings": [
                    {"account": expense, "units": "12.34", "currency": "CNY"},
                    {"account": asset, "units": "-12.34", "currency": "CNY"},
                ],
            }
        ],
        "confidence": 0.99,
        "rationale_summary": "disposable E2E copy",
    }
    draft = kernel.submit_draft(body, "e2e-main-1", agent_id="e2e-agent")
    draft_id = draft["draft"]["draft_id"]
    duplicate = kernel.submit_draft(body, "e2e-main-1", agent_id="e2e-agent")
    check("idempotent submit", duplicate["draft"]["draft_id"] == draft_id, draft_id)
    check("draft did not write ledger", len(kernel.list_observations()) == 0)

    validation = kernel.validate_draft(draft_id)
    report["validation"] = {
        "status": validation["status"],
        "checks": [item["code"] for item in validation["checks"]],
        "review_digest": validation.get("review_digest"),
    }
    check("dry-run passes", validation["status"] == "pass", validation["status"])
    check("review digest present", bool(validation.get("review_digest")))

    kernel.approve_draft(draft_id)
    commit = kernel.commit_draft(draft_id)
    report["commit"] = commit
    status = kernel.get_status()
    check("commit recorded", bool(commit.get("commit_id")), commit.get("commit_id"))
    check("status clean after commit", status["status"] == "Clean", status["status"])
    check("no observation for own commit", status["observations"] == 0, status["observations"])
    check("audit chain intact", status["audit_integrity"]["ok"], status["audit_integrity"])

    entries, errors, _ = loader.load_file(str(main_bean))
    matches = [
        entry
        for entry in entries
        if isinstance(entry, data.Transaction) and entry.narration == "LedgerKernel end-to-end check"
    ]
    check("committed transaction appears once", len(matches) == 1, len(matches))
    check("reload has no errors", len(errors) == 0, len(errors))
    check("committed payee preserved", bool(matches) and matches[0].payee == "E2E Shop")

    # 1) External edit of a non-target file: detected, diffed, attributed unknown.
    append_block(
        external_file,
        external_transaction(day + dt.timedelta(days=1), asset, expense, "external edit one"),
    )
    scan = kernel.scan_ledger()
    report["external_scan_1"] = scan
    check("external change detected", scan["observations"] == 1, scan["observations"])
    observation = kernel.get_observation(scan["last_observation_id"]) if scan.get("last_observation_id") else None
    if observation:
        check("external change attributed unknown", observation["observation"]["source_kind"] == "external_unknown")
        check("external diff captured", bool(observation.get("diff")), (observation.get("diff") or "")[:80])
    else:
        check("external observation stored", False)

    # 2) Draft validated before the next external edit must go stale.
    body2 = {
        "schema_version": 1,
        "operation": "append_transactions",
        "transactions": [
            {
                "date": (day + dt.timedelta(days=2)).isoformat(),
                "flag": "*",
                "payee": "E2E Shop",
                "narration": "stale draft candidate",
                "postings": [
                    {"account": expense, "units": "5.00", "currency": "CNY"},
                    {"account": asset, "units": "-5.00", "currency": "CNY"},
                ],
            }
        ],
        "confidence": 0.99,
        "rationale_summary": "stale draft check",
    }
    draft2 = kernel.submit_draft(body2, "e2e-stale-1", agent_id="e2e-agent")
    draft2_id = draft2["draft"]["draft_id"]
    kernel.validate_draft(draft2_id)
    append_block(
        external_file,
        external_transaction(day + dt.timedelta(days=3), asset, expense, "external edit two"),
    )
    scan = kernel.scan_ledger()
    report["external_scan_2"] = scan
    check("second external change detected", scan["observations"] == 2, scan["observations"])
    stale_state = kernel.get_draft(draft2_id)["workflow"]
    check("draft invalidated by external change", bool(stale_state.get("stale")), stale_state.get("stale_reason"))
    try:
        kernel.approve_draft(draft2_id)
    except KernelError as exc:
        check("stale draft cannot be approved", exc.code == "STALE_REVISION", exc.code)
    else:
        check("stale draft cannot be approved", False, "approve unexpectedly succeeded")

    # 3) An external edit that breaks the ledger blocks controlled commits.
    body3 = {
        "schema_version": 1,
        "operation": "append_transactions",
        "transactions": [
            {
                "date": (day + dt.timedelta(days=4)).isoformat(),
                "flag": "*",
                "payee": "E2E Shop",
                "narration": "blocked by invalid ledger",
                "postings": [
                    {"account": expense, "units": "3.00", "currency": "CNY"},
                    {"account": asset, "units": "-3.00", "currency": "CNY"},
                ],
            }
        ],
        "confidence": 0.99,
        "rationale_summary": "invalid ledger gate check",
    }
    draft3_id = kernel.submit_draft(body3, "e2e-invalid-1", agent_id="e2e-agent")["draft"]["draft_id"]
    kernel.validate_draft(draft3_id)
    kernel.approve_draft(draft3_id)
    append_block(
        external_file,
        (
            f"{day + dt.timedelta(days=5)} * \"External Editor\" \"unbalanced edit\"\n"
            f"  {expense}    10.00 CNY\n"
            f"  {asset}      -9.00 CNY\n"
        ),
    )
    scan = kernel.scan_ledger()
    report["invalid_scan"] = scan
    check("invalid ledger detected", scan["status"] == "LedgerInvalid", scan["status"])
    check("invalid ledger reported", bool(scan["beancount_errors"]), scan["beancount_errors"][:1])
    try:
        kernel.commit_draft(draft3_id)
    except KernelError as exc:
        check("commit blocked by invalid ledger", exc.code == "LEDGER_INVALID", exc.code)
    else:
        check("commit blocked by invalid ledger", False, "commit unexpectedly succeeded")

    recovery = kernel.recovery_status()
    report["recovery"] = recovery
    check("no pending recovery", recovery["status"] == "Clean", recovery["status"])
    kernel.close()

    failed = [item for item in checks if not item["ok"]]
    report["checks"] = checks
    report["failed"] = failed
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        for item in checks:
            print(("PASS " if item["ok"] else "FAIL ") + item["check"])
        print(json.dumps({key: report[key] for key in ("source", "fixture", "init", "commit")}, ensure_ascii=False, indent=2, default=str))
        if failed:
            print(f"{len(failed)} check(s) failed", file=sys.stderr)

    if args.keep:
        print(f"kept: {workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
