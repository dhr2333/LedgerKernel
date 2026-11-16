from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from ledger_kernel.kernel import Kernel

ACCOUNTS = """\
2020-01-01 open Assets:Cash CNY
2020-01-01 open Assets:Bank CNY
2020-01-01 open Expenses:Food CNY
2020-01-01 open Expenses:Books CNY
2020-01-01 open Income:Salary CNY
2020-01-01 open Equity:Opening CNY
"""

OPENING = """\
2026-01-01 * "Opening" "Initial balance"
  Assets:Cash  1000.00 CNY
  Equity:Opening  -1000.00 CNY
"""

TARGET = "transactions/kernel-import.bean"


def make_ledger(base: Path, *, target: str = TARGET, invalid: bool = False,
                target_header: str = "; kernel import target\n") -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "accounts.bean").write_text(ACCOUNTS, encoding="utf-8")
    (base / "transactions").mkdir(exist_ok=True)
    (base / "transactions" / "2026.bean").write_text(OPENING, encoding="utf-8")
    target_path = base / target
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(target_header, encoding="utf-8")
    extra = "\n2026-01-03 open This Is Not A Directive\n" if invalid else ""
    (base / "main.bean").write_text(
        'option "operating_currency" "CNY"\n'
        'include "accounts.bean"\n'
        'include "transactions/2026.bean"\n'
        f'include "{target}"\n' + extra,
        encoding="utf-8",
    )
    return base / "main.bean"


def make_kernel(base: Path, **kwargs: Any) -> Kernel:
    root = make_ledger(base)
    return Kernel.init(root, TARGET, **kwargs)


def payload(
    *,
    date: str = "2026-01-02",
    narration: str = "Lunch",
    payee: str | None = "Shop",
    account: str = "Expenses:Food",
    counter_account: str = "Assets:Cash",
    amount: str = "25.00",
    currency: str = "CNY",
    tags: list[str] | None = None,
    confidence: float | None = 0.9,
) -> dict[str, Any]:
    transaction: dict[str, Any] = {
        "date": date,
        "narration": narration,
        "postings": [
            {"account": account, "units": amount, "currency": currency},
            {"account": counter_account, "units": f"-{amount}" if not amount.startswith("-") else amount[1:], "currency": currency},
        ],
    }
    if payee is not None:
        transaction["payee"] = payee
    if tags:
        transaction["tags"] = tags
    body: dict[str, Any] = {
        "schema_version": 1,
        "operation": "append_transactions",
        "transactions": [transaction],
        "confidence": confidence,
        "rationale_summary": "fixture",
    }
    return body


def approve(kernel: Kernel, draft_id: str) -> dict[str, Any]:
    return kernel.validate_draft(draft_id) and kernel.approve_draft(draft_id)
