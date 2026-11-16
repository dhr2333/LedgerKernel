"""Shared fake kernel CLI runners and payloads for the TUI tests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ledger_review_tui.kernel_cli import CommandResult

LEDGER = "/tmp/review-tui-ledger"

BY_ACCOUNT: dict[str, Any] = {
    "mode": "by_account",
    "account": None,
    "note": None,
    "groups": [
        {
            "account": "Assets:Bank",
            "lifecycle": "open",
            "balances": {"CNY": "1000.00"},
            "reconciliation": {
                "status": "current",
                "next_due": None,
                "difference_status": "none",
            },
            "contract": None,
            "pending_drafts": [
                {
                    "draft_id": "drf_1",
                    "date": "2026-01-02",
                    "amount": "120.00",
                    "currency": "CNY",
                    "narration": "Books",
                    "state": "NeedsHumanApproval",
                    "validation_status": "pass",
                }
            ],
            "warnings": [
                {
                    "draft_id": "drf_1",
                    "code": "CONTRACT_CURRENCY",
                    "severity": "warning",
                    "account": "Assets:Bank",
                    "message": "currency not allowed",
                }
            ],
            "draft_count": 1,
        },
        {
            "account": "Expenses:Books",
            "lifecycle": "open",
            "balances": {},
            "reconciliation": {
                "status": "not_configured",
                "next_due": None,
                "difference_status": "none",
            },
            "contract": None,
            "pending_drafts": [],
            "warnings": [],
            "draft_count": 0,
        },
    ],
}


class RecordingRunner:
    """Runner double: records argv and returns queued results in order."""

    def __init__(self, *results: CommandResult) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> CommandResult:
        self.calls.append(list(argv))
        return self.results.pop(0) if self.results else CommandResult(0, "{}")
