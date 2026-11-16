from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from ledger_kernel.cli import main as cli_main
from ledger_kernel.kernel import Kernel
from ledger_kernel.mcp_server import McpServer, TOOLS, serve

from .helpers import TARGET, make_kernel, make_ledger, payload


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = cli_main(list(args))
        return code, output.getvalue()

    def test_init_and_status(self) -> None:
        root = make_ledger(self.base)
        code, output = self.run_cli("--json", "init", str(root), "--write-target", TARGET)
        self.assertEqual(code, 0, output)
        data = json.loads(output)
        self.assertEqual(data["status"], "Clean")
        code, output = self.run_cli("--ledger", str(self.base), "--json", "status")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["ledger_id"], data["ledger_id"])

    def test_approve_requires_explicit_confirmation(self) -> None:
        kernel = make_kernel(self.base)
        draft_id = kernel.submit_draft(payload(), "confirm", agent_id="a")["draft"]["draft_id"]
        kernel.validate_draft(draft_id)
        code, _ = self.run_cli("--ledger", str(self.base), "--json", "drafts", "approve", draft_id)
        self.assertEqual(code, 2)
        self.assertEqual(kernel.get_draft(draft_id)["workflow"]["state"], "NeedsHumanApproval")
        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "drafts", "approve", draft_id, "--confirm", draft_id
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(kernel.get_draft(draft_id)["workflow"]["state"], "Approved")

    def test_review_lists_and_approves_a_pending_batch(self) -> None:
        kernel = make_kernel(self.base)
        draft_ids = []
        for index, narration in enumerate(("Lunch", "Books")):
            draft_id = kernel.submit_draft(
                payload(narration=narration, date=f"2026-01-0{2 + index}"),
                f"review-{index}",
                agent_id="a",
            )["draft"]["draft_id"]
            kernel.validate_draft(draft_id)
            draft_ids.append(draft_id)

        code, output = self.run_cli("--ledger", str(self.base), "--json", "review")
        self.assertEqual(code, 0, output)
        queue = json.loads(output)
        self.assertEqual({row["draft_id"] for row in queue["drafts"]}, set(draft_ids))

        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "review", *draft_ids,
            "--approve", "--confirm", ",".join(draft_ids),
        )
        self.assertEqual(code, 0, output)
        for draft_id in draft_ids:
            self.assertEqual(kernel.get_draft(draft_id)["workflow"]["state"], "Approved")

    def test_draft_template_expands_repeated_fields(self) -> None:
        kernel = make_kernel(self.base)
        (self.base / ".ledger-kernel" / "templates.yaml").write_text(
            "schema_version: 1\n"
            "templates:\n"
            "  electricity:\n"
            "    payee: Electric Utility\n"
            "    narration: Monthly power\n"
            "    tags: [Family, Fixed]\n"
            "    postings:\n"
            "      - account: Expenses:Food\n"
            "        units: amount\n"
            "      - account: Assets:Cash\n"
            "        units: -amount\n",
            encoding="utf-8",
        )
        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "drafts", "submit",
            "--template", "electricity", "--amount", "99.90", "--date", "2026-01-05", "--key", "tpl-1",
        )
        self.assertEqual(code, 0, output)
        draft = json.loads(output)["draft"]
        transaction = draft["transactions"][0]
        self.assertEqual(transaction["payee"], "Electric Utility")
        self.assertEqual(transaction["tags"], ["Family", "Fixed"])
        self.assertEqual(transaction["postings"][0]["units"], "99.90")
        self.assertEqual(transaction["postings"][1]["units"], "-99.90")

    def test_unknown_template_is_rejected(self) -> None:
        make_kernel(self.base)
        code, _ = self.run_cli(
            "--ledger", str(self.base), "drafts", "submit", "--template", "missing", "--amount", "1.00"
        )
        self.assertEqual(code, 2)

    def test_cli_reports_errors_as_json(self) -> None:
        code, output = self.run_cli("--ledger", str(self.base), "--json", "status")
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output)["ok"])


class McpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.kernel = make_kernel(self.base)
        self.server = McpServer(self.kernel)

    def tearDown(self) -> None:
        self.kernel.close()
        self.tmp.cleanup()

    def test_initialize_and_tool_list(self) -> None:
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        response = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(names, {tool["name"] for tool in TOOLS})
        self.assertNotIn("ledger_approve_draft", names)
        self.assertNotIn("ledger_commit", names)

    def test_submit_and_read_tools(self) -> None:
        response = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "ledger_submit_draft",
                    "arguments": {"payload": payload(), "idempotency_key": "mcp-1", "agent_id": "mcp-agent"},
                },
            }
        )
        result = response["result"]
        self.assertFalse(result["isError"])
        draft_id = json.loads(result["content"][0]["text"])["draft_id"]
        response = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "ledger_get_draft_status", "arguments": {"draft_id": draft_id}},
            }
        )
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(json.loads(response["result"]["content"][0]["text"])["state"], "Submitted")

    def test_stdio_serve_round_trip(self) -> None:
        stdin = io.StringIO('{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}\n')
        stdout = io.StringIO()
        serve(self.kernel, stdin, stdout)
        response = json.loads(stdout.getvalue().strip())
        self.assertEqual(response["result"]["serverInfo"]["name"], "ledger-kernel")

    def test_tool_error_is_reported_without_writing(self) -> None:
        response = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "ledger_submit_draft",
                    "arguments": {
                        "payload": {"schema_version": 1, "transactions": [{"narration": "x"}]},
                        "idempotency_key": "bad",
                    },
                },
            }
        )
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(len(self.kernel.list_drafts()), 0)


if __name__ == "__main__":
    unittest.main()
