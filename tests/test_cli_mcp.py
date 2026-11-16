from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ledger_kernel.cli import main as cli_main
from ledger_kernel.kernel import Kernel
from ledger_kernel.mcp_server import McpServer, TOOLS, serve

from .helpers import TARGET, make_kernel, make_ledger, payload

REVIEW_CONTRACT = """\
schema_version: 1
accounts:
  Assets:Cash:
    policy:
      allowed_agents: [a]
      allowed_currencies: [CNY]
      reconciliation:
        cadence: monthly
"""


def make_review_kernel(base: Path) -> Kernel:
    """Standard fixture kernel with a small contract seeded before init."""
    meta = base / ".ledger-kernel"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "contracts.yaml").write_text(REVIEW_CONTRACT, encoding="utf-8")
    return make_kernel(base)


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


class ReviewByAccountTests(unittest.TestCase):
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

    def submit(
        self,
        kernel: Kernel,
        *,
        key: str,
        narration: str = "Lunch",
        account: str = "Expenses:Food",
        counter_account: str = "Assets:Cash",
        validate: bool = True,
    ) -> str:
        draft_id = kernel.submit_draft(
            payload(narration=narration, account=account, counter_account=counter_account),
            key,
            agent_id="a",
        )["draft"]["draft_id"]
        if validate:
            kernel.validate_draft(draft_id)
        return draft_id

    def two_draft_kernel(self) -> tuple[Kernel, str, str]:
        kernel = make_review_kernel(self.base)
        draft_a = self.submit(kernel, key="acct-a")
        draft_b = self.submit(
            kernel,
            key="acct-b",
            narration="Books",
            account="Expenses:Books",
            counter_account="Assets:Bank",
        )
        return kernel, draft_a, draft_b

    def review(self, *extra: str) -> dict:
        code, output = self.run_cli("--ledger", str(self.base), "--json", "review", *extra)
        self.assertEqual(code, 0, output)
        return json.loads(output)

    def test_by_account_groups_each_draft_under_its_accounts(self) -> None:
        kernel, draft_a, draft_b = self.two_draft_kernel()
        data = self.review("--by-account")
        self.assertEqual(data["mode"], "by_account")
        self.assertIsNone(data["account"])
        self.assertIsNone(data["note"])
        groups = {group["account"]: group for group in data["groups"]}
        self.assertEqual(
            set(groups), {"Assets:Bank", "Assets:Cash", "Expenses:Books", "Expenses:Food"}
        )
        self.assertEqual(
            [row["draft_id"] for row in groups["Expenses:Food"]["pending_drafts"]], [draft_a]
        )
        self.assertEqual(
            [row["draft_id"] for row in groups["Expenses:Books"]["pending_drafts"]], [draft_b]
        )
        # A draft appears under every account in its account_refs.
        self.assertEqual(
            [row["draft_id"] for row in groups["Assets:Cash"]["pending_drafts"]], [draft_a]
        )
        self.assertEqual(
            [row["draft_id"] for row in groups["Assets:Bank"]["pending_drafts"]], [draft_b]
        )
        for group in data["groups"]:
            self.assertEqual(group["draft_count"], len(group["pending_drafts"]))
        row = groups["Expenses:Food"]["pending_drafts"][0]
        self.assertEqual(
            set(row),
            {"draft_id", "date", "amount", "currency", "narration", "state", "validation_status"},
        )
        self.assertEqual(row["date"], "2026-01-02")
        self.assertEqual(row["amount"], "25.00")
        self.assertEqual(row["currency"], "CNY")
        self.assertEqual(row["narration"], "Lunch")
        self.assertEqual(row["state"], "NeedsHumanApproval")
        self.assertEqual(row["validation_status"], "pass_with_warnings")
        # Viewing is read-only: no draft was approved or otherwise changed.
        self.assertEqual(kernel.get_draft(draft_a)["workflow"]["state"], "NeedsHumanApproval")
        self.assertEqual(kernel.get_draft(draft_b)["workflow"]["state"], "NeedsHumanApproval")

    def test_account_filter_limits_to_one_group(self) -> None:
        kernel, draft_a, draft_b = self.two_draft_kernel()
        data = self.review("--by-account", "--account", "Assets:Cash")
        self.assertEqual(data["mode"], "by_account")
        self.assertEqual(data["account"], "Assets:Cash")
        self.assertEqual([group["account"] for group in data["groups"]], ["Assets:Cash"])
        self.assertEqual(
            [row["draft_id"] for row in data["groups"][0]["pending_drafts"]], [draft_a]
        )
        # --account implies the grouped view even without --by-account.
        data = self.review("--account", "Expenses:Books")
        self.assertEqual(data["mode"], "by_account")
        self.assertEqual([group["account"] for group in data["groups"]], ["Expenses:Books"])
        self.assertEqual(
            [row["draft_id"] for row in data["groups"][0]["pending_drafts"]], [draft_b]
        )

    def test_account_filter_shows_group_without_drafts(self) -> None:
        self.two_draft_kernel()
        data = self.review("--account", "Income:Salary")
        self.assertEqual(len(data["groups"]), 1)
        group = data["groups"][0]
        self.assertEqual(group["account"], "Income:Salary")
        self.assertEqual(group["lifecycle"], "open")
        self.assertEqual(group["balances"], {})
        self.assertIsNone(group["contract"])
        self.assertEqual(group["pending_drafts"], [])
        self.assertEqual(group["draft_count"], 0)
        self.assertEqual(group["warnings"], [])
        self.assertEqual(group["reconciliation"]["status"], "not_configured")

    def test_unknown_account_returns_empty_groups_and_note(self) -> None:
        self.two_draft_kernel()
        data = self.review("--account", "Expenses:Missing")
        self.assertEqual(data["mode"], "by_account")
        self.assertEqual(data["groups"], [])
        self.assertEqual(data["account"], "Expenses:Missing")
        self.assertIn("unknown account", data["note"])
        self.assertIn("Expenses:Missing", data["note"])
        code, text = self.run_cli(
            "--ledger", str(self.base), "review", "--account", "Expenses:Missing"
        )
        self.assertEqual(code, 0, text)
        self.assertIn("unknown account: Expenses:Missing", text)

    def test_groups_show_contract_summary_and_contract_warnings(self) -> None:
        kernel, draft_a, draft_b = self.two_draft_kernel()
        groups = {group["account"]: group for group in self.review("--by-account")["groups"]}
        cash = groups["Assets:Cash"]
        self.assertEqual(cash["contract"]["allowed_agents"], ["a"])
        self.assertEqual(cash["contract"]["allowed_currencies"], ["CNY"])
        self.assertEqual(cash["contract"]["reconciliation"]["cadence"], "monthly")
        self.assertEqual(cash["reconciliation"]["status"], "never")
        self.assertIsNone(cash["reconciliation"]["next_due"])
        warning_codes = [warning["code"] for warning in cash["warnings"]]
        self.assertIn("CONTRACT_RECONCILIATION", warning_codes)
        for warning in cash["warnings"]:
            self.assertEqual(warning["severity"], "warning")
            self.assertEqual(warning["draft_id"], draft_a)
            self.assertEqual(warning["account"], "Assets:Cash")
        # An account without a contract is explicit in the group view.
        self.assertIsNone(groups["Expenses:Food"]["contract"])
        self.assertEqual(groups["Expenses:Food"]["warnings"], [])
        self.assertEqual(groups["Expenses:Food"]["reconciliation"]["status"], "not_configured")
        self.assertEqual(
            groups["Expenses:Books"]["pending_drafts"][0]["draft_id"], draft_b
        )
        # A contract warning never becomes a blocking check in the grouping view.
        entry = kernel.get_draft(draft_a)
        self.assertEqual(entry["validation"]["status"], "pass_with_warnings")
        self.assertEqual(entry["workflow"]["state"], "NeedsHumanApproval")

    def test_group_reflects_reconciliation_status(self) -> None:
        kernel = make_review_kernel(self.base)
        today = dt.date.today()
        kernel.reconcile_account("Assets:Cash", as_of=today)
        draft_id = self.submit(kernel, key="reconciled")
        groups = {group["account"]: group for group in self.review("--by-account")["groups"]}
        cash = groups["Assets:Cash"]
        self.assertEqual(cash["reconciliation"]["status"], "current")
        self.assertEqual(
            cash["reconciliation"]["next_due"],
            (today + dt.timedelta(days=31)).isoformat(),
        )
        self.assertEqual(cash["reconciliation"]["difference_status"], "none")
        self.assertEqual(cash["warnings"], [])
        self.assertEqual(cash["pending_drafts"][0]["draft_id"], draft_id)
        self.assertEqual(cash["pending_drafts"][0]["validation_status"], "pass")

    def test_state_filter_still_applies_to_groups(self) -> None:
        kernel = make_review_kernel(self.base)
        submitted = self.submit(
            kernel,
            key="submitted",
            narration="Books",
            account="Expenses:Books",
            counter_account="Assets:Bank",
            validate=False,
        )
        self.submit(kernel, key="validated")
        groups = {group["account"]: group for group in self.review("--by-account")["groups"]}
        self.assertEqual(set(groups), {"Assets:Bank", "Assets:Cash", "Expenses:Books", "Expenses:Food"})
        filtered = {
            group["account"]: group
            for group in self.review("--by-account", "--state", "Submitted")["groups"]
        }
        self.assertEqual(set(filtered), {"Assets:Bank", "Expenses:Books"})
        row = filtered["Expenses:Books"]["pending_drafts"][0]
        self.assertEqual(row["draft_id"], submitted)
        self.assertIsNone(row["validation_status"])
        data = self.review("--account", "Expenses:Food", "--state", "Submitted")
        self.assertEqual(len(data["groups"]), 1)
        self.assertEqual(data["groups"][0]["draft_count"], 0)

    def test_by_account_approve_requires_explicit_ids_and_binds_each_draft(self) -> None:
        kernel, draft_a, draft_b = self.two_draft_kernel()
        before_events = [event["event_type"] for event in kernel.store.audit_events()]
        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "review", "--by-account", "--approve"
        )
        self.assertEqual(code, 2, output)
        self.assertEqual(json.loads(output)["error"]["code"], "INVALID_STATE")
        for draft_id in (draft_a, draft_b):
            self.assertEqual(kernel.get_draft(draft_id)["workflow"]["state"], "NeedsHumanApproval")
        with mock.patch("sys.stdin", io.StringIO("")):
            code, output = self.run_cli(
                "--ledger",
                str(self.base),
                "--json",
                "review",
                draft_a,
                draft_b,
                "--by-account",
                "--approve",
            )
        self.assertEqual(code, 2, output)
        self.assertEqual(json.loads(output)["error"]["code"], "INVALID_STATE")
        self.assertEqual([event["event_type"] for event in kernel.store.audit_events()], before_events)
        draft_ids = [draft_a, draft_b]
        code, output = self.run_cli(
            "--ledger",
            str(self.base),
            "--json",
            "review",
            *draft_ids,
            "--by-account",
            "--account",
            "Assets:Cash",
            "--approve",
            "--confirm",
            ",".join(draft_ids),
        )
        self.assertEqual(code, 0, output)
        approved = json.loads(output)["approved"]
        self.assertEqual({row["draft_id"] for row in approved}, set(draft_ids))
        for draft_id in draft_ids:
            entry = kernel.get_draft(draft_id)
            self.assertEqual(entry["workflow"]["state"], "Approved")
            self.assertEqual(
                entry["workflow"]["approval"]["validation_id"],
                entry["validation"]["validation_id"],
            )
            self.assertEqual(
                entry["workflow"]["approval"]["actor"], {"type": "human", "id": "local-user"}
            )
        new_events = kernel.store.audit_events()[len(before_events):]
        # Grouping adds no audit events: only the per-draft v0.1 approval events appear.
        self.assertEqual({event["event_type"] for event in new_events}, {"DraftApproved"})
        by_draft = {event["draft_id"]: event for event in new_events}
        for draft_id in draft_ids:
            validation = kernel.get_draft(draft_id)["validation"]
            event = by_draft[draft_id]
            self.assertEqual(event["validation_id"], validation["validation_id"])
            self.assertEqual(event["ledger_revision"], validation["ledger_revision"])
            self.assertEqual(event["contract_revision"], validation["contract_revision"])

    def test_grouped_view_is_read_only_and_keeps_existing_flows(self) -> None:
        kernel, draft_a, draft_b = self.two_draft_kernel()
        before_audit = [event["event_type"] for event in kernel.store.audit_events()]
        before_revision = kernel.get_status()["ledger_revision"]
        code, queue_before = self.run_cli("--ledger", str(self.base), "--json", "review")
        self.assertEqual(code, 0, queue_before)
        code, grouped_text = self.run_cli("--ledger", str(self.base), "review", "--by-account")
        self.assertEqual(code, 0, grouped_text)
        code, queue_after = self.run_cli("--ledger", str(self.base), "--json", "review")
        self.assertEqual(code, 0, queue_after)
        self.assertEqual(queue_before, queue_after)
        self.assertEqual(json.loads(queue_after)["mode"], "queue")
        self.assertEqual([event["event_type"] for event in kernel.store.audit_events()], before_audit)
        self.assertEqual(kernel.get_status()["ledger_revision"], before_revision)
        code, detail = self.run_cli("--ledger", str(self.base), "--json", "review", draft_a)
        self.assertEqual(code, 0, detail)
        self.assertEqual(json.loads(detail)["mode"], "detail")
        for draft_id in (draft_a, draft_b):
            self.assertEqual(kernel.get_draft(draft_id)["workflow"]["state"], "NeedsHumanApproval")

    def test_grouped_view_cannot_be_combined_with_explicit_draft_ids(self) -> None:
        _kernel, draft_a, _draft_b = self.two_draft_kernel()
        for flag in (["--by-account"], ["--account", "Assets:Cash"]):
            code, output = self.run_cli(
                "--ledger", str(self.base), "--json", "review", draft_a, *flag
            )
            self.assertEqual(code, 2, output)
            self.assertEqual(json.loads(output)["error"]["code"], "INVALID_STATE")

    def test_by_account_with_no_drafts(self) -> None:
        make_review_kernel(self.base)
        data = self.review("--by-account")
        self.assertEqual(data["groups"], [])
        self.assertIsNone(data["note"])
        code, text = self.run_cli("--ledger", str(self.base), "review", "--by-account")
        self.assertEqual(code, 0, text)
        self.assertIn("no pending drafts", text)

    def test_grouped_view_text_rendering(self) -> None:
        _kernel, draft_a, _draft_b = self.two_draft_kernel()
        code, text = self.run_cli("--ledger", str(self.base), "review", "--by-account")
        self.assertEqual(code, 0, text)
        lines = text.splitlines()
        self.assertIn("== Assets:Cash  open  1000.00 CNY  reconciliation never (next -)", lines)
        self.assertTrue(
            any(
                "contract: yes" in line
                and "allowed_agents: a" in line
                and "allowed_currencies: CNY" in line
                for line in lines
            )
        )
        self.assertTrue(any("contract: none" in line for line in lines))
        self.assertTrue(
            any(
                f"pending: {draft_a}" in line
                and "2026-01-02" in line
                and "25.00 CNY" in line
                and "Lunch" in line
                for line in lines
            )
        )
        self.assertTrue(
            any("warning: CONTRACT_RECONCILIATION" in line and draft_a in line for line in lines)
        )
        code, text = self.run_cli(
            "--ledger", str(self.base), "review", "--account", "Income:Salary"
        )
        self.assertEqual(code, 0, text)
        lines = text.splitlines()
        self.assertIn("== Income:Salary  open  -  reconciliation not_configured (next -)", lines)
        self.assertIn("pending: none", [line.strip() for line in lines])


if __name__ == "__main__":
    unittest.main()
