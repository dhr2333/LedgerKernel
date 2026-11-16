from __future__ import annotations

import contextlib
import io
import unittest
from typing import Any

from ledger_review_tui.__main__ import main
from ledger_review_tui.kernel_cli import KernelCliError

from .helpers import BY_ACCOUNT


class FakeClient:
    def __init__(
        self, payload: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.payload = payload if payload is not None else {"groups": []}
        self.error = error
        self.calls: list[str] = []

    def review_by_account(self) -> dict[str, Any]:
        self.calls.append("review_by_account")
        if self.error is not None:
            raise self.error
        return self.payload

    def approve_drafts(self, draft_ids: list[str], confirm: str) -> list[dict[str, Any]]:
        raise AssertionError("--list must never call approve_drafts")


class ListModeTests(unittest.TestCase):
    def run_main(
        self, client: FakeClient, *extra: str
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        args = ["--ledger", "/tmp/ledger", "--list", *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(
                args,
                client_factory=lambda ledger, kernel_cmd: client,
            )
        return code, out.getvalue(), err.getvalue()

    def test_list_prints_grouped_queue_without_subprocess(self) -> None:
        client = FakeClient(BY_ACCOUNT)
        code, out, err = self.run_main(client)
        self.assertEqual(code, 0, err)
        self.assertEqual(client.calls, ["review_by_account"])
        self.assertIn("== Assets:Bank", out)
        self.assertIn("drf_1", out)
        self.assertIn("[NeedsHumanApproval / pass]", out)
        self.assertIn("warning: CONTRACT_CURRENCY", out)
        self.assertIn("== Expenses:Books", out)

    def test_list_maps_cli_errors_to_exit_two(self) -> None:
        client = FakeClient(
            error=KernelCliError("ledger not initialized", code="INVALID_STATE")
        )
        code, out, err = self.run_main(client)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("ledger not initialized", err)

    def test_client_factory_receives_ledger_and_kernel_cmd(self) -> None:
        seen: list[tuple[str, str]] = []

        def factory(ledger: str, kernel_cmd: str) -> FakeClient:
            seen.append((ledger, kernel_cmd))
            return FakeClient({"groups": []})

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(
                [
                    "--ledger",
                    "/tmp/ledger",
                    "--kernel-cmd",
                    "ledger-kernel-x",
                    "--list",
                ],
                client_factory=factory,
            )
        self.assertEqual(code, 0)
        self.assertEqual(seen, [("/tmp/ledger", "ledger-kernel-x")])
        self.assertIn("no pending drafts", out.getvalue())

    def test_list_does_not_require_curses_import(self) -> None:
        import sys

        self.assertNotIn("ledger_review_tui.tui", sys.modules)


if __name__ == "__main__":
    unittest.main()
