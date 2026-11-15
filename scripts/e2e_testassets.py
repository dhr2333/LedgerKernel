#!/usr/bin/env python3
"""End-to-end check on a disposable copy of the desensitized TestAssets ledger.

Usage:
    PYTHONPATH=src python3 scripts/e2e_testassets.py [--source PATH] [--keep]

The real ledger is never modified: everything happens in a temp directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path

from beancount import loader
from beancount.core import data

from ledger_kernel.kernel import Kernel

DEFAULT_SOURCE = Path("/home/dhr2333/Syncthing/Bill/TestAssets")
TARGET = "transactions/kernel-import.bean"


def choose_accounts(root: Path) -> tuple[str, str, dt.date]:
    entries, errors, _ = loader.load_file(str(root))
    if errors:
        raise SystemExit(f"source ledger has {len(errors)} errors; E2E requires a clean baseline")
    asserted = {entry.account for entry in entries if isinstance(entry, data.Balance)}
    opened = {entry.account for entry in entries if isinstance(entry, data.Open)}
    closed = {entry.account for entry in entries if isinstance(entry, data.Close)}
    dates = [entry.date for entry in entries]

    from collections import Counter

    usage: Counter = Counter()
    for entry in entries:
        if isinstance(entry, data.Transaction):
            for posting in entry.postings:
                usage[posting.account] += 1

    expenses = sorted(
        (
            account
            for account in opened - closed - asserted
            if account.startswith("Expenses:") and ":" in account
        ),
        key=lambda account: (-usage[account], account),
    )
    assets = sorted(
        (
            account
            for account in opened - closed - asserted
            if account.startswith("Assets:") and ":" in account
        ),
        key=lambda account: (-usage[account], account),
    )
    if not expenses or not assets:
        raise SystemExit("could not find an unasserted expense/asset account pair")
    return assets[0], expenses[0], max(dates) + dt.timedelta(days=1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="ledger-kernel-e2e-"))
    ledger = workdir / "Assets"
    shutil.copytree(
        args.source,
        ledger,
        ignore=shutil.ignore_patterns(".git", ".stversions", ".stfolder", "__pycache__"),
    )
    (ledger / "transactions").mkdir(exist_ok=True)
    (ledger / TARGET).write_text("; LedgerKernel E2E import target\n", encoding="utf-8")
    main_bean = ledger / "main.bean"
    main_bean.write_text(main_bean.read_text(encoding="utf-8") + f'\ninclude "{TARGET}"\n', encoding="utf-8")

    asset, expense, date = choose_accounts(main_bean)
    report: dict = {"ledger": str(ledger), "accounts": [asset, expense], "date": date.isoformat()}

    kernel = Kernel.init(main_bean, TARGET)
    report["init_status"] = kernel.get_status()["status"]
    report["closure_files"] = kernel.get_status()["files_in_closure"]
    report["account_count"] = len(kernel.list_accounts())

    body = {
        "schema_version": 1,
        "operation": "append_transactions",
        "transactions": [
            {
                "date": date.isoformat(),
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
    draft = kernel.submit_draft(body, "e2e-testassets-1", agent_id="e2e-agent")
    draft_id = draft["draft"]["draft_id"]
    validation = kernel.validate_draft(draft_id)
    report["validation"] = validation["status"]
    report["checks"] = [check["code"] for check in validation["checks"]]
    if validation["status"] == "blocked":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("E2E aborted: candidate blocked", file=sys.stderr)
        return 1

    kernel.approve_draft(draft_id)
    commit = kernel.commit_draft(draft_id)
    report["commit_id"] = commit["commit_id"]
    report["after_status"] = kernel.get_status()["status"]

    entries, errors, _ = loader.load_file(str(main_bean))
    matches = [
        entry
        for entry in entries
        if isinstance(entry, data.Transaction) and entry.narration == "LedgerKernel end-to-end check"
    ]
    report["errors_after"] = len(errors)
    report["matching_transactions"] = len(matches)
    report["committed_payee"] = matches[0].payee if matches else None
    report["observations"] = len(kernel.list_observations())
    report["audit_events"] = len(kernel.audit_tail(500))
    print(json.dumps(report, ensure_ascii=False, indent=2))

    ok = (
        report["matching_transactions"] == 1
        and report["errors_after"] == 0
        and report["after_status"] == "Clean"
        and report["committed_payee"] == "E2E Shop"
    )
    if args.keep:
        print(f"kept: {workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
