from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ledger_kernel.cli import main as cli_main
from ledger_kernel.errors import KernelError
from ledger_kernel.kernel import Kernel
from ledger_kernel.mcp_server import TOOLS
from ledger_kernel.revision import contract_revision

from .helpers import TARGET, make_ledger, payload

MONTHLY_CONTRACT = """\
schema_version: 1
accounts:
  Assets:Cash:
    policy:
      reconciliation:
        cadence: monthly
"""

WEEKLY_HARD_DEADLINE_CONTRACT = """\
schema_version: 1
accounts:
  Assets:Bank:
    policy:
      reconciliation:
        cadence: weekly
        hard_deadline: true
"""

NO_CADENCE_CONTRACT = """\
schema_version: 1
accounts:
  Assets:Bank:
    policy: {}
"""

MULTI_ACCOUNTS = """
2020-01-01 open Assets:Multi CNY,USD
2020-01-01 open Equity:OpeningUSD USD
"""

MULTI_TRANSACTIONS = """
2026-01-02 * "Multi" "cny leg"
  Assets:Multi   100.00 CNY
  Equity:Opening  -100.00 CNY

2026-01-03 * "Multi" "usd leg"
  Assets:Multi   25.00 USD
  Equity:OpeningUSD  -25.00 USD
"""


def make_reconciliation_kernel(
    base: Path,
    *,
    contract: str | None = None,
    extra_accounts: str = "",
    extra_transactions: str = "",
    extra_root: str = "",
) -> Kernel:
    """Build the standard fixture kernel, optionally pre-seeding contracts/entries."""
    if contract is not None:
        meta = base / ".ledger-kernel"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "contracts.yaml").write_text(contract, encoding="utf-8")
    root = make_ledger(base)
    if extra_accounts:
        path = base / "accounts.bean"
        path.write_text(path.read_text(encoding="utf-8") + extra_accounts, encoding="utf-8")
    if extra_transactions:
        path = base / "transactions" / "2026.bean"
        path.write_text(path.read_text(encoding="utf-8") + extra_transactions, encoding="utf-8")
    if extra_root:
        path = base / "main.bean"
        path.write_text(path.read_text(encoding="utf-8") + extra_root, encoding="utf-8")
    return Kernel.init(root, TARGET)


def reconciliation_events(kernel: Kernel, event_type: str | None = None) -> list[dict]:
    events = [
        event
        for event in kernel.store.audit_events()
        if str(event.get("event_type", "")).startswith("AccountReconciliation")
        or event.get("event_type") == "AccountReconciled"
    ]
    if event_type is not None:
        events = [event for event in events if event.get("event_type") == event_type]
    return events


def ledger_files(base: Path) -> dict[str, bytes]:
    return {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in sorted(base.rglob("*"))
        if path.is_file() and ".ledger-kernel" not in path.relative_to(base).parts
    }


@contextlib.contextmanager
def non_interactive_stdin():
    original = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        yield
    finally:
        sys.stdin = original


class _ReconciliationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.today = dt.date.today()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def new_kernel(self, **kwargs) -> Kernel:
        return make_reconciliation_kernel(self.base, **kwargs)

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = cli_main(list(args))
        return code, output.getvalue()

    def assert_reconciliation_invalid(self, func, *args, **kwargs) -> KernelError:
        with self.assertRaises(KernelError) as caught:
            func(*args, **kwargs)
        self.assertEqual(caught.exception.code, "RECONCILIATION_INVALID")
        return caught.exception

    def assert_no_reconciliation_written(self, kernel: Kernel) -> None:
        self.assertEqual(reconciliation_events(kernel, "AccountReconciled"), [])
        state = kernel.store.load_state() or {}
        self.assertEqual(state.get("reconciliations") or [], [])


class ReconciliationRecordTests(_ReconciliationTestCase):
    def test_record_success_persists_record_and_audit_event(self) -> None:
        kernel = self.new_kernel()
        before_revision = kernel.get_status()["ledger_revision"]

        result = kernel.reconcile_account(
            "Assets:Cash", as_of=self.today, note="与银行 App 一致"
        )
        record = result["record"]

        self.assertTrue(record["reconciliation_id"].startswith("rec_"))
        self.assertEqual(record["account"], "Assets:Cash")
        self.assertEqual(record["as_of"], self.today.isoformat())
        self.assertEqual(record["ledger_balances"], {"CNY": "1000.00"})
        self.assertIsNone(record["declared_balances"])
        self.assertIsNone(record["differences"])
        self.assertEqual(record["declared_not_in_ledger"], [])
        self.assertEqual(record["actor"], {"type": "human", "id": "local-user"})
        self.assertEqual(record["ledger_revision"], before_revision)
        self.assertEqual(record["contract_revision"], contract_revision(kernel.config.contracts_path))
        self.assertEqual(record["note"], "与银行 App 一致")
        self.assertTrue(record["recorded_at"].endswith("Z"))
        self.assertEqual(record["status"], "valid")

        self.assertEqual(result["reconciliation"]["status"], "not_configured")
        self.assertEqual(result["reconciliation"]["difference_status"], "none")

        events = reconciliation_events(kernel, "AccountReconciled")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["reconciliation_id"], record["reconciliation_id"])
        self.assertEqual(event["account"], "Assets:Cash")
        self.assertEqual(event["as_of"], self.today.isoformat())
        self.assertEqual(event["ledger_balances"], {"CNY": "1000.00"})
        self.assertEqual(event["actor"], {"type": "human", "id": "local-user"})
        self.assertEqual(result["audit_event_id"], event["event_id"])
        self.assertTrue(kernel.store.verify_audit()["ok"])

        # The reconciliation does not change the ledger revision but it is
        # cached in state.json (the cache is derived, the audit is the truth).
        self.assertEqual(kernel.get_status()["ledger_revision"], before_revision)
        cached = kernel.store.load_state()["reconciliations"]
        self.assertEqual([row["reconciliation_id"] for row in cached], [record["reconciliation_id"]])
        self.assertEqual(cached[0]["status"], "valid")

    def test_record_does_not_write_the_ledger(self) -> None:
        kernel = self.new_kernel()
        before = ledger_files(self.base)
        before_revision = kernel.get_status()["ledger_revision"]

        kernel.reconcile_account("Assets:Cash", as_of=self.today)

        self.assertEqual(ledger_files(self.base), before)
        self.assertEqual(kernel.get_status()["ledger_revision"], before_revision)

    def test_zero_difference_is_recorded(self) -> None:
        kernel = self.new_kernel()
        result = kernel.reconcile_account(
            "Assets:Cash", as_of=self.today, declared={"CNY": Decimal("1000.00")}
        )
        record = result["record"]
        self.assertEqual(record["declared_balances"], {"CNY": "1000.00"})
        self.assertEqual(Decimal(record["differences"]["CNY"]), Decimal("0"))
        self.assertEqual(record["declared_not_in_ledger"], [])
        self.assertEqual(result["reconciliation"]["difference_status"], "zero")

    def test_empty_declared_mapping_is_treated_as_undeclared(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        record = kernel.reconcile_account("Assets:Cash", as_of=self.today, declared={})["record"]
        self.assertIsNone(record["declared_balances"])
        self.assertIsNone(record["differences"])
        self.assertEqual(record["declared_not_in_ledger"], [])

    def test_nonzero_difference_is_recorded(self) -> None:
        kernel = self.new_kernel()
        result = kernel.reconcile_account(
            "Assets:Cash", as_of=self.today, declared={"CNY": "999.50"}
        )
        record = result["record"]
        self.assertEqual(record["declared_balances"], {"CNY": "999.50"})
        self.assertEqual(Decimal(record["differences"]["CNY"]), Decimal("-0.50"))
        self.assertEqual(result["reconciliation"]["difference_status"], "nonzero")

    def test_declared_currency_absent_from_ledger_uses_zero(self) -> None:
        kernel = self.new_kernel()
        result = kernel.reconcile_account(
            "Assets:Cash", as_of=self.today, declared={"USD": "10.00"}
        )
        record = result["record"]
        self.assertEqual(record["ledger_balances"], {"CNY": "1000.00"})
        self.assertEqual(record["declared_balances"], {"USD": "10.00"})
        self.assertEqual(Decimal(record["differences"]["USD"]), Decimal("10.00"))
        self.assertEqual(record["declared_not_in_ledger"], ["USD"])
        self.assertEqual(result["reconciliation"]["difference_status"], "nonzero")

    def test_multi_currency_balances_and_differences(self) -> None:
        kernel = self.new_kernel(
            extra_accounts=MULTI_ACCOUNTS, extra_transactions=MULTI_TRANSACTIONS
        )
        result = kernel.reconcile_account(
            "Assets:Multi",
            as_of=self.today,
            declared={"CNY": Decimal("100.00"), "USD": Decimal("20.00")},
        )
        record = result["record"]
        self.assertEqual(record["ledger_balances"], {"CNY": "100.00", "USD": "25.00"})
        self.assertEqual(record["declared_balances"], {"CNY": "100.00", "USD": "20.00"})
        self.assertEqual(Decimal(record["differences"]["CNY"]), Decimal("0"))
        self.assertEqual(Decimal(record["differences"]["USD"]), Decimal("-5.00"))
        self.assertEqual(record["declared_not_in_ledger"], [])
        self.assertEqual(result["reconciliation"]["difference_status"], "nonzero")

    def test_declared_amounts_reject_float_and_non_decimal(self) -> None:
        kernel = self.new_kernel()
        cases = {
            "float": {"CNY": 1000.0},
            "bool": {"CNY": True},
            "non-numeric string": {"CNY": "abc"},
            "nan": {"CNY": Decimal("NaN")},
            "infinity": {"CNY": Decimal("Infinity")},
            "not a mapping": [("CNY", Decimal("1"))],
        }
        for label, declared in cases.items():
            with self.subTest(label=label):
                self.assert_reconciliation_invalid(
                    kernel.reconcile_account,
                    "Assets:Cash",
                    as_of=self.today,
                    declared=declared,
                )
        self.assert_no_reconciliation_written(kernel)

    def test_duplicate_as_of_is_rejected(self) -> None:
        kernel = self.new_kernel()
        first = kernel.reconcile_account("Assets:Cash", as_of=self.today)
        self.assert_reconciliation_invalid(
            kernel.reconcile_account, "Assets:Cash", as_of=self.today
        )
        self.assertEqual(len(reconciliation_events(kernel, "AccountReconciled")), 1)
        rows = kernel.list_reconciliations("Assets:Cash")
        self.assertEqual([row["reconciliation_id"] for row in rows], [first["record"]["reconciliation_id"]])

    def test_future_as_of_is_rejected(self) -> None:
        kernel = self.new_kernel()
        self.assert_reconciliation_invalid(
            kernel.reconcile_account, "Assets:Cash", as_of=self.today + dt.timedelta(days=1)
        )
        self.assert_no_reconciliation_written(kernel)

    def test_unknown_account_is_rejected(self) -> None:
        kernel = self.new_kernel()
        self.assert_reconciliation_invalid(
            kernel.reconcile_account, "Assets:Missing", as_of=self.today
        )
        self.assert_no_reconciliation_written(kernel)

    def test_account_not_open_on_as_of_is_rejected(self) -> None:
        open_date = self.today - dt.timedelta(days=100)
        as_of = self.today - dt.timedelta(days=200)
        kernel = self.new_kernel(
            extra_accounts=f"\n{open_date.isoformat()} open Assets:Future CNY\n"
        )
        self.assert_reconciliation_invalid(
            kernel.reconcile_account, "Assets:Future", as_of=as_of
        )
        self.assert_no_reconciliation_written(kernel)

    def test_closed_account_is_rejected(self) -> None:
        close_date = self.today - dt.timedelta(days=10)
        as_of = self.today - dt.timedelta(days=1)
        kernel = self.new_kernel(
            extra_accounts=f"\n{close_date.isoformat()} close Expenses:Books\n"
        )
        self.assert_reconciliation_invalid(
            kernel.reconcile_account, "Expenses:Books", as_of=as_of
        )
        self.assert_no_reconciliation_written(kernel)

    def test_invalid_ledger_is_rejected(self) -> None:
        kernel = self.new_kernel()
        path = self.base / "transactions" / "2026.bean"
        path.write_text(
            path.read_text(encoding="utf-8")
            + '\n2026-01-03 * "External" "unbalanced edit"\n  Expenses:Food  10.00 CNY\n  Assets:Cash  -9.00 CNY\n',
            encoding="utf-8",
        )
        self.assert_reconciliation_invalid(
            kernel.reconcile_account, "Assets:Cash", as_of=self.today
        )
        self.assert_no_reconciliation_written(kernel)
        self.assertEqual(kernel.get_status()["status"], "LedgerInvalid")

    def test_invalid_note_is_rejected_without_writing(self) -> None:
        kernel = self.new_kernel()
        self.assert_reconciliation_invalid(
            kernel.reconcile_account, "Assets:Cash", as_of=self.today, note=123  # type: ignore[arg-type]
        )
        self.assert_no_reconciliation_written(kernel)


class ReconciliationStateTests(_ReconciliationTestCase):
    def test_never_and_not_configured_statuses(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        view = kernel.get_account("Assets:Cash")["reconciliation"]
        self.assertEqual(view["status"], "never")
        self.assertIsNone(view["last"])
        self.assertIsNone(view["next_due"])
        self.assertEqual(view["difference_status"], "none")
        self.assertEqual(
            kernel.get_account("Expenses:Food")["reconciliation"]["status"], "not_configured"
        )

    def test_current_status_and_next_due(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        result = kernel.reconcile_account("Assets:Cash", as_of=self.today)
        view = result["reconciliation"]
        self.assertEqual(view["status"], "current")
        self.assertEqual(
            view["next_due"], (self.today + dt.timedelta(days=31)).isoformat()
        )
        self.assertEqual(view["last"]["reconciliation_id"], result["record"]["reconciliation_id"])
        self.assertEqual(view["last"]["as_of"], self.today.isoformat())
        self.assertEqual(kernel.get_account("Assets:Cash")["reconciliation"], view)

    def test_overdue_status_and_next_due(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        as_of = self.today - dt.timedelta(days=40)
        result = kernel.reconcile_account("Assets:Cash", as_of=as_of)
        view = result["reconciliation"]
        self.assertEqual(view["status"], "overdue")
        self.assertEqual(view["next_due"], (as_of + dt.timedelta(days=31)).isoformat())
        self.assertEqual(kernel.get_account("Assets:Cash")["reconciliation"]["status"], "overdue")

    def test_not_configured_with_record_keeps_difference_status(self) -> None:
        kernel = self.new_kernel(contract=NO_CADENCE_CONTRACT)
        result = kernel.reconcile_account(
            "Assets:Bank", as_of=self.today, declared={"CNY": "0.00"}
        )
        self.assertEqual(result["reconciliation"]["status"], "not_configured")
        self.assertIsNone(result["reconciliation"]["next_due"])
        self.assertEqual(result["reconciliation"]["difference_status"], "zero")

    def test_contract_warning_on_never_and_silent_when_current(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)

        first = kernel.submit_draft(payload(), "never-warning", agent_id="a")["draft"]["draft_id"]
        validation = kernel.validate_draft(first)
        codes = {check["code"] for check in validation["checks"]}
        self.assertIn("CONTRACT_RECONCILIATION", codes)
        self.assertEqual(validation["status"], "pass_with_warnings")

        kernel.reconcile_account("Assets:Cash", as_of=self.today)

        second = kernel.submit_draft(
            payload(narration="after reconcile"), "current-warning", agent_id="a"
        )["draft"]["draft_id"]
        validation = kernel.validate_draft(second)
        codes = {check["code"] for check in validation["checks"]}
        self.assertNotIn("CONTRACT_RECONCILIATION", codes)

    def test_reconciliation_survives_later_ledger_changes(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        record = kernel.reconcile_account("Assets:Cash", as_of=self.today)["record"]

        draft_id = kernel.submit_draft(
            payload(narration="after reconciliation"), "after-reconcile", agent_id="a"
        )["draft"]["draft_id"]
        kernel.validate_draft(draft_id)
        kernel.approve_draft(draft_id)
        kernel.commit_draft(draft_id)
        self.assertNotEqual(
            kernel.get_status()["ledger_revision"], record["ledger_revision"]
        )

        rows = kernel.list_reconciliations("Assets:Cash")
        self.assertEqual([row["reconciliation_id"] for row in rows], [record["reconciliation_id"]])
        self.assertEqual(kernel.get_account("Assets:Cash")["reconciliation"]["status"], "current")

    def test_overdue_warning_still_does_not_block_commit(self) -> None:
        kernel = self.new_kernel(contract=WEEKLY_HARD_DEADLINE_CONTRACT)
        kernel.reconcile_account("Assets:Bank", as_of=self.today - dt.timedelta(days=40))

        draft_id = kernel.submit_draft(
            payload(account="Expenses:Books", counter_account="Assets:Bank"),
            "hard-deadline-ignored",
            agent_id="a",
        )["draft"]["draft_id"]
        validation = kernel.validate_draft(draft_id)
        codes = [check["code"] for check in validation["checks"]]
        self.assertIn("CONTRACT_RECONCILIATION", codes)
        self.assertNotEqual(validation["status"], "blocked")
        kernel.approve_draft(draft_id)
        result = kernel.commit_draft(draft_id)
        self.assertTrue(result["commit_id"].startswith("cmt_"))
        self.assertEqual(kernel.get_draft(draft_id)["workflow"]["state"], "Committed")


class ReconciliationRevertTests(_ReconciliationTestCase):
    def seed_two(self, kernel: Kernel) -> tuple[dict, dict]:
        old = kernel.reconcile_account(
            "Assets:Cash", as_of=self.today - dt.timedelta(days=40)
        )["record"]
        new = kernel.reconcile_account("Assets:Cash", as_of=self.today)["record"]
        return old, new

    def test_revert_requires_the_latest_record(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        old, _new = self.seed_two(kernel)

        self.assert_reconciliation_invalid(
            kernel.revert_reconciliation, old["reconciliation_id"], reason="too old"
        )
        self.assertEqual(len(reconciliation_events(kernel, "AccountReconciled")), 2)
        self.assertEqual(reconciliation_events(kernel, "AccountReconciliationReverted"), [])

    def test_revert_unknown_or_already_reverted_id_is_rejected(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        _old, new = self.seed_two(kernel)
        self.assert_reconciliation_invalid(
            kernel.revert_reconciliation, "rec_missing", reason="unknown"
        )
        kernel.revert_reconciliation(new["reconciliation_id"], reason="wrong balance")
        self.assert_reconciliation_invalid(
            kernel.revert_reconciliation, new["reconciliation_id"], reason="again"
        )

    def test_revert_requires_reason(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        _old, new = self.seed_two(kernel)
        self.assert_reconciliation_invalid(
            kernel.revert_reconciliation, new["reconciliation_id"], reason=""
        )
        self.assertEqual(reconciliation_events(kernel, "AccountReconciliationReverted"), [])

    def test_revert_restores_previous_status_and_keeps_history(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        old, new = self.seed_two(kernel)
        before_revision = kernel.get_status()["ledger_revision"]

        result = kernel.revert_reconciliation(new["reconciliation_id"], reason="抄错")

        self.assertEqual(result["reconciliation_id"], new["reconciliation_id"])
        self.assertEqual(result["account"], "Assets:Cash")
        self.assertEqual(result["reason"], "抄错")
        self.assertTrue(result["audit_event_id"].startswith("evt_"))
        view = result["reconciliation"]
        self.assertEqual(view["status"], "overdue")
        self.assertEqual(view["last"]["reconciliation_id"], old["reconciliation_id"])
        self.assertEqual(
            view["next_due"],
            ((self.today - dt.timedelta(days=40)) + dt.timedelta(days=31)).isoformat(),
        )

        events = reconciliation_events(kernel)
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "AccountReconciled",
                "AccountReconciled",
                "AccountReconciliationReverted",
            ],
        )
        revert_event = events[-1]
        self.assertEqual(revert_event["reconciliation_id"], new["reconciliation_id"])
        self.assertEqual(revert_event["reason"], "抄错")
        self.assertEqual(revert_event["actor"], {"type": "human", "id": "local-user"})
        self.assertTrue(kernel.store.verify_audit()["ok"])
        self.assertEqual(kernel.get_status()["ledger_revision"], before_revision)

        cached = {row["reconciliation_id"]: row for row in kernel.store.load_state()["reconciliations"]}
        self.assertEqual(cached[new["reconciliation_id"]]["status"], "reverted")
        self.assertEqual(cached[new["reconciliation_id"]]["revert_reason"], "抄错")

        rows = kernel.list_reconciliations("Assets:Cash")
        self.assertEqual([row["reconciliation_id"] for row in rows], [old["reconciliation_id"]])

    def test_same_as_of_can_be_recorded_after_revert(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        _old, new = self.seed_two(kernel)
        kernel.revert_reconciliation(new["reconciliation_id"], reason="recheck")
        replacement = kernel.reconcile_account("Assets:Cash", as_of=self.today)
        self.assertNotEqual(
            replacement["record"]["reconciliation_id"], new["reconciliation_id"]
        )
        self.assertEqual(replacement["reconciliation"]["status"], "current")
        self.assertEqual(len(kernel.list_reconciliations("Assets:Cash")), 2)

    def test_list_reconciliations_empty_for_unknown_account(self) -> None:
        kernel = self.new_kernel()
        self.assertEqual(kernel.list_reconciliations("Assets:Missing"), [])


class ReconciliationCacheTests(_ReconciliationTestCase):
    def test_missing_state_json_rebuilds_cache_from_audit(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        record = kernel.reconcile_account(
            "Assets:Cash", as_of=self.today, declared={"CNY": "999.50"}
        )["record"]
        kernel.store.state_path.unlink()

        reopened = Kernel.open(self.base)
        rows = reopened.list_reconciliations("Assets:Cash")
        self.assertEqual([row["reconciliation_id"] for row in rows], [record["reconciliation_id"]])
        state = reopened.store.load_state()
        self.assertEqual(
            [row["reconciliation_id"] for row in state["reconciliations"]],
            [record["reconciliation_id"]],
        )
        self.assertEqual(state["ledger_revision"], record["ledger_revision"])
        self.assertTrue(reopened.store.verify_audit()["ok"])

    def test_corrupt_state_cache_rebuilds_from_audit(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        record = kernel.reconcile_account(
            "Assets:Cash", as_of=self.today, declared={"CNY": "1000.00"}
        )["record"]
        state = kernel.store.load_state()
        state["reconciliations"] = {"not": "a list"}
        kernel.store.save_state(state)

        reopened = Kernel.open(self.base)
        rows = reopened.list_reconciliations("Assets:Cash")
        self.assertEqual([row["reconciliation_id"] for row in rows], [record["reconciliation_id"]])
        repaired = reopened.store.load_state()["reconciliations"]
        self.assertIsInstance(repaired, list)
        self.assertEqual(repaired[0]["reconciliation_id"], record["reconciliation_id"])
        self.assertEqual(repaired[0]["declared_balances"], {"CNY": "1000.00"})

    def test_reads_derive_truth_from_audit_not_from_cache(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        record = kernel.reconcile_account("Assets:Cash", as_of=self.today)["record"]
        state = kernel.store.load_state()
        state["reconciliations"] = []  # structurally valid but stale cache
        kernel.store.save_state(state)

        reopened = Kernel.open(self.base)
        self.assertEqual(
            [row["reconciliation_id"] for row in reopened.list_reconciliations("Assets:Cash")],
            [record["reconciliation_id"]],
        )
        self.assertEqual(
            reopened.get_account("Assets:Cash")["reconciliation"]["status"], "current"
        )


class ReconciliationAccountViewTests(_ReconciliationTestCase):
    def test_assertion_summary_is_exposed(self) -> None:
        kernel = self.new_kernel(
            extra_accounts="\n2026-01-02 balance Assets:Cash 1000.00 CNY\n"
        )
        account = kernel.get_account("Assets:Cash")
        self.assertEqual(account["assertion_status"], "asserted")
        self.assertEqual(account["last_assertion"]["date"], "2026-01-02")
        self.assertEqual(account["last_assertion"]["amounts"], {"CNY": "1000.00"})

    def test_unset_assertion_is_reported_as_null(self) -> None:
        kernel = self.new_kernel()
        account = kernel.get_account("Assets:Bank")
        self.assertEqual(account["assertion_status"], "unset")
        self.assertIsNone(account["last_assertion"])

    def test_failed_assertion_is_global_ledger_invalid(self) -> None:
        kernel = self.new_kernel(
            extra_accounts="\n2026-01-02 balance Assets:Cash 999.00 CNY\n"
        )
        self.assertEqual(kernel.get_status()["status"], "LedgerInvalid")
        account = kernel.get_account("Assets:Cash")
        self.assertEqual(account["assertion_status"], "asserted")
        self.assertEqual(account["last_assertion"]["amounts"], {"CNY": "999.00"})

    def test_get_account_combines_assertion_and_reconciliation(self) -> None:
        kernel = self.new_kernel(
            contract=MONTHLY_CONTRACT,
            extra_accounts="\n2026-01-02 balance Assets:Cash 1000.00 CNY\n",
        )
        kernel.reconcile_account("Assets:Cash", as_of=self.today)
        account = kernel.get_account("Assets:Cash")
        self.assertEqual(account["assertion_status"], "asserted")
        self.assertEqual(account["reconciliation"]["status"], "current")


class ReconciliationCliTests(_ReconciliationTestCase):
    def test_cli_reconcile_requires_confirm(self) -> None:
        self.new_kernel(contract=MONTHLY_CONTRACT)
        with non_interactive_stdin():
            code, output = self.run_cli(
                "--ledger",
                str(self.base),
                "--json",
                "accounts",
                "reconcile",
                "Assets:Cash",
                "--as-of",
                self.today.isoformat(),
            )
        self.assertEqual(code, 2, output)
        self.assertEqual(json.loads(output)["error"]["code"], "INVALID_STATE")
        self.assert_no_reconciliation_written(Kernel.open(self.base))

    def test_cli_reconcile_rejects_wrong_confirm(self) -> None:
        self.new_kernel(contract=MONTHLY_CONTRACT)
        code, output = self.run_cli(
            "--ledger",
            str(self.base),
            "--json",
            "accounts",
            "reconcile",
            "Assets:Cash",
            "--as-of",
            self.today.isoformat(),
            "--confirm",
            "Assets:Bank",
        )
        self.assertEqual(code, 2, output)
        self.assertEqual(json.loads(output)["error"]["code"], "INVALID_STATE")
        self.assert_no_reconciliation_written(Kernel.open(self.base))

    def test_cli_reconcile_success_list_and_account_view(self) -> None:
        self.new_kernel(contract=MONTHLY_CONTRACT)
        code, output = self.run_cli(
            "--ledger",
            str(self.base),
            "--json",
            "accounts",
            "reconcile",
            "Assets:Cash",
            "--as-of",
            self.today.isoformat(),
            "--declared",
            "CNY=1000.00",
            "--declared",
            "USD=10.00",
            "--note",
            "cli reconcile",
            "--confirm",
            "Assets:Cash",
        )
        self.assertEqual(code, 0, output)
        data = json.loads(output)
        reconciliation_id = data["record"]["reconciliation_id"]
        self.assertTrue(reconciliation_id.startswith("rec_"))
        self.assertEqual(
            data["record"]["declared_balances"], {"CNY": "1000.00", "USD": "10.00"}
        )
        self.assertEqual(data["record"]["declared_not_in_ledger"], ["USD"])
        self.assertEqual(Decimal(data["record"]["differences"]["CNY"]), Decimal("0"))
        self.assertEqual(Decimal(data["record"]["differences"]["USD"]), Decimal("10.00"))
        self.assertEqual(data["record"]["note"], "cli reconcile")
        self.assertEqual(data["reconciliation"]["status"], "current")

        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "accounts", "reconciliation", "list", "Assets:Cash"
        )
        self.assertEqual(code, 0, output)
        rows = json.loads(output)
        self.assertEqual([row["reconciliation_id"] for row in rows], [reconciliation_id])
        self.assertEqual(rows[0]["status"], "valid")

        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "accounts", "show", "Assets:Cash"
        )
        self.assertEqual(code, 0, output)
        account = json.loads(output)
        self.assertEqual(account["reconciliation"]["status"], "current")
        self.assertEqual(account["assertion_status"], "unset")

    def test_cli_reconcile_rejects_future_as_of(self) -> None:
        self.new_kernel(contract=MONTHLY_CONTRACT)
        code, output = self.run_cli(
            "--ledger",
            str(self.base),
            "--json",
            "accounts",
            "reconcile",
            "Assets:Cash",
            "--as-of",
            (self.today + dt.timedelta(days=1)).isoformat(),
            "--confirm",
            "Assets:Cash",
        )
        self.assertEqual(code, 2, output)
        self.assertEqual(json.loads(output)["error"]["code"], "RECONCILIATION_INVALID")
        self.assert_no_reconciliation_written(Kernel.open(self.base))

    def test_cli_reconcile_rejects_invalid_declared_amount(self) -> None:
        self.new_kernel(contract=MONTHLY_CONTRACT)
        code, output = self.run_cli(
            "--ledger",
            str(self.base),
            "--json",
            "accounts",
            "reconcile",
            "Assets:Cash",
            "--as-of",
            self.today.isoformat(),
            "--declared",
            "CNY=not-a-number",
            "--confirm",
            "Assets:Cash",
        )
        self.assertEqual(code, 2, output)
        self.assertEqual(json.loads(output)["error"]["code"], "RECONCILIATION_INVALID")
        self.assert_no_reconciliation_written(Kernel.open(self.base))

    def test_cli_reconciliation_revert_requires_confirm(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        record = kernel.reconcile_account("Assets:Cash", as_of=self.today)["record"]
        with non_interactive_stdin():
            code, output = self.run_cli(
                "--ledger",
                str(self.base),
                "--json",
                "accounts",
                "reconciliation",
                "revert",
                record["reconciliation_id"],
                "--reason",
                "typo",
            )
        self.assertEqual(code, 2, output)
        self.assertEqual(json.loads(output)["error"]["code"], "INVALID_STATE")
        self.assertEqual(len(reconciliation_events(Kernel.open(self.base), "AccountReconciled")), 1)
        self.assertEqual(len(kernel.list_reconciliations("Assets:Cash")), 1)

    def test_cli_reconciliation_revert_success_rolls_status_back(self) -> None:
        kernel = self.new_kernel(contract=MONTHLY_CONTRACT)
        old = kernel.reconcile_account(
            "Assets:Cash", as_of=self.today - dt.timedelta(days=40)
        )["record"]
        new = kernel.reconcile_account("Assets:Cash", as_of=self.today)["record"]

        code, output = self.run_cli(
            "--ledger",
            str(self.base),
            "--json",
            "accounts",
            "reconciliation",
            "revert",
            new["reconciliation_id"],
            "--reason",
            "抄错",
            "--confirm",
            new["reconciliation_id"],
        )
        self.assertEqual(code, 0, output)
        data = json.loads(output)
        self.assertEqual(data["reconciliation"]["status"], "overdue")
        self.assertEqual(data["reconciliation"]["last"]["reconciliation_id"], old["reconciliation_id"])

        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "accounts", "reconciliation", "list", "Assets:Cash"
        )
        self.assertEqual(code, 0, output)
        rows = json.loads(output)
        self.assertEqual([row["reconciliation_id"] for row in rows], [old["reconciliation_id"]])

    def test_cli_text_output_renders_reconciliation(self) -> None:
        self.new_kernel(contract=MONTHLY_CONTRACT)
        code, output = self.run_cli(
            "--ledger",
            str(self.base),
            "accounts",
            "reconcile",
            "Assets:Cash",
            "--as-of",
            self.today.isoformat(),
            "--declared",
            "CNY=999.50",
            "--confirm",
            "Assets:Cash",
        )
        self.assertEqual(code, 0, output)
        self.assertIn("rec_", output)
        self.assertIn("current", output)
        self.assertIn("999.50", output)

        code, listing = self.run_cli(
            "--ledger", str(self.base), "accounts", "reconciliation", "list", "Assets:Cash"
        )
        self.assertEqual(code, 0, listing)
        self.assertIn("rec_", listing)

    def test_mcp_does_not_expose_reconciliation_write_tools(self) -> None:
        names = {tool["name"] for tool in TOOLS}
        self.assertFalse([name for name in names if "reconcil" in name])


if __name__ == "__main__":
    unittest.main()
