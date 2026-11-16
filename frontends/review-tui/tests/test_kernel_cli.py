from __future__ import annotations

import unittest

from ledger_review_tui.kernel_cli import CommandResult, KernelCli, KernelCliError
from ledger_review_tui.model import confirm_string

from .helpers import LEDGER, RecordingRunner


def make_client(runner: RecordingRunner, kernel_cmd: str = "ledger-kernel") -> KernelCli:
    return KernelCli(ledger=LEDGER, kernel_cmd=kernel_cmd, runner=runner)


class CommandShapeTests(unittest.TestCase):
    def test_review_queue_uses_fixed_prefix_and_review(self) -> None:
        runner = RecordingRunner(
            CommandResult(0, '{"mode": "queue", "drafts": []}')
        )
        payload = make_client(runner).review_queue()
        self.assertEqual(payload["mode"], "queue")
        self.assertEqual(
            runner.calls,
            [["ledger-kernel", "--ledger", LEDGER, "--json", "review"]],
        )

    def test_review_by_account_adds_flag(self) -> None:
        runner = RecordingRunner(
            CommandResult(0, '{"mode": "by_account", "groups": []}')
        )
        payload = make_client(runner).review_by_account()
        self.assertEqual(payload["mode"], "by_account")
        self.assertEqual(
            runner.calls[0][:4],
            ["ledger-kernel", "--ledger", LEDGER, "--json"],
        )
        self.assertEqual(
            runner.calls[0][4:], ["review", "--by-account"]
        )

    def test_draft_detail_uses_review_detail(self) -> None:
        runner = RecordingRunner(
            CommandResult(
                0, '{"mode": "detail", "drafts": [{"draft_id": "drf_1"}]}'
            )
        )
        detail = make_client(runner).draft_detail("drf_1")
        self.assertEqual(detail["draft_id"], "drf_1")
        self.assertEqual(
            runner.calls[0],
            ["ledger-kernel", "--ledger", LEDGER, "--json", "review", "drf_1"],
        )

    def test_validate_draft_uses_drafts_validate(self) -> None:
        runner = RecordingRunner(
            CommandResult(0, '{"validation_id": "val_1", "status": "pass"}')
        )
        validation = make_client(runner).validate_draft("drf_1")
        self.assertEqual(validation["validation_id"], "val_1")
        self.assertEqual(
            runner.calls[0],
            [
                "ledger-kernel",
                "--ledger",
                LEDGER,
                "--json",
                "drafts",
                "validate",
                "drf_1",
            ],
        )

    def test_custom_kernel_command_is_first_argv(self) -> None:
        runner = RecordingRunner(CommandResult(0, "{}"))
        make_client(runner, kernel_cmd="ledger-kernel-0.2").review_queue()
        self.assertEqual(runner.calls[0][0], "ledger-kernel-0.2")


class ApprovalTests(unittest.TestCase):
    def test_approve_passes_explicit_confirmation(self) -> None:
        runner = RecordingRunner(
            CommandResult(0, '[{"draft_id": "drf_1", "workflow": {}}]')
        )
        approved = make_client(runner).approve_drafts(["drf_1"], "drf_1")
        self.assertEqual(approved[0]["draft_id"], "drf_1")
        self.assertEqual(
            runner.calls[0],
            [
                "ledger-kernel",
                "--ledger",
                LEDGER,
                "--json",
                "drafts",
                "approve",
                "drf_1",
                "--confirm",
                "drf_1",
            ],
        )

    def test_approve_multiple_uses_comma_joined_confirmation(self) -> None:
        runner = RecordingRunner(CommandResult(0, "[]"))
        expected = confirm_string(["drf_1", "drf_2"])
        self.assertEqual(expected, "drf_1,drf_2")
        make_client(runner).approve_drafts(["drf_1", "drf_2"], expected)
        self.assertEqual(
            runner.calls[0][4:],
            ["drafts", "approve", "drf_1", "drf_2", "--confirm", expected],
        )

    def test_mismatched_confirmation_never_calls_kernel(self) -> None:
        runner = RecordingRunner()
        with self.assertRaises(KernelCliError) as caught:
            make_client(runner).approve_drafts(["drf_1"], "yes")
        self.assertEqual(caught.exception.code, "INVALID_STATE")
        self.assertEqual(runner.calls, [])

    def test_empty_draft_ids_never_calls_kernel(self) -> None:
        runner = RecordingRunner()
        with self.assertRaises(KernelCliError):
            make_client(runner).approve_drafts([], "")
        self.assertEqual(runner.calls, [])


class ErrorMappingTests(unittest.TestCase):
    def test_nonzero_exit_with_error_json_is_mapped(self) -> None:
        runner = RecordingRunner(
            CommandResult(
                2,
                '{"ok": false, "error": {"code": "INVALID_STATE", '
                '"message": "no such draft"}}',
            )
        )
        with self.assertRaises(KernelCliError) as caught:
            make_client(runner).review_queue()
        self.assertEqual(caught.exception.code, "INVALID_STATE")
        self.assertIn("no such draft", str(caught.exception))

    def test_nonzero_exit_without_json_reports_exit_code(self) -> None:
        runner = RecordingRunner(
            CommandResult(127, "", "bash: ledger-kernel: command not found")
        )
        with self.assertRaises(KernelCliError) as caught:
            make_client(runner).review_queue()
        self.assertIsNone(caught.exception.code)
        self.assertIn("127", str(caught.exception))
        self.assertIn("command not found", str(caught.exception))

    def test_ok_false_with_zero_exit_is_an_error(self) -> None:
        runner = RecordingRunner(
            CommandResult(
                0, '{"ok": false, "error": {"code": "X", "message": "bad"}}'
            )
        )
        with self.assertRaises(KernelCliError) as caught:
            make_client(runner).review_queue()
        self.assertEqual(caught.exception.code, "X")

    def test_invalid_json_is_an_error(self) -> None:
        runner = RecordingRunner(CommandResult(0, "not json"))
        with self.assertRaises(KernelCliError):
            make_client(runner).review_queue()

    def test_unexpected_payload_type_is_an_error(self) -> None:
        runner = RecordingRunner(CommandResult(0, "[]"))
        with self.assertRaises(KernelCliError):
            make_client(runner).review_queue()

    def test_missing_detail_is_an_error(self) -> None:
        runner = RecordingRunner(
            CommandResult(0, '{"mode": "detail", "drafts": []}')
        )
        with self.assertRaises(KernelCliError):
            make_client(runner).draft_detail("drf_missing")


if __name__ == "__main__":
    unittest.main()
