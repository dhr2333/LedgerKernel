from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from ledger_kernel.adapter import BeancountAdapter
from ledger_kernel.cli import main as cli_main
from ledger_kernel.errors import KernelError
from ledger_kernel.registry import (
    load_registry,
    parse_registry_text,
    plan_registry,
    registry_revision,
    registry_view,
)

from .helpers import make_kernel


def registry_data() -> dict:
    return {
        "schema_version": 2,
        "groups": [
            {"id": "grp_assets", "alias": "资产", "segment": "Assets", "parent": None, "type": "Assets"},
            {"id": "grp_savings", "alias": "储蓄", "segment": "Savings", "parent": "grp_assets"},
            {"id": "grp_bank", "alias": "银行", "segment": "Bank", "parent": "grp_savings"},
            {"id": "grp_boc", "alias": "中国银行", "segment": "BOC", "parent": "grp_bank"},
            {"id": "grp_expenses", "alias": "支出", "segment": "Expenses", "parent": None, "type": "Expenses"},
            {"id": "grp_food", "alias": "餐饮组", "segment": "Food", "parent": "grp_expenses"},
        ],
        "accounts": [
            {
                "id": "acc_card",
                "alias": "中行工资卡",
                "segment": "Acct-7f3a2c1d",
                "mode": "opaque",
                "parent": "grp_boc",
                "status": "active",
                "currencies": ["CNY"],
            },
            {
                "id": "acc_food",
                "alias": "餐饮",
                "segment": "Dining",
                "mode": "semantic",
                "parent": "grp_food",
                "status": "active",
            },
        ],
    }


def text_of(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


LEDGER = """\
2020-01-01 open Assets:Savings:Bank:BOC:Acct-7f3a2c1d CNY
  alias: "中行工资卡"
2020-01-01 open Expenses:Food:Dining CNY
"""


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def parse(self, data: dict):
        return parse_registry_text(text_of(data))

    def load(self, body: str = LEDGER):
        (self.base / "accounts.bean").write_text(body, encoding="utf-8")
        (self.base / "main.bean").write_text(
            'option "operating_currency" "CNY"\ninclude "accounts.bean"\n',
            encoding="utf-8",
        )
        return BeancountAdapter(self.base).load(self.base / "main.bean")

    def assert_invalid(self, data: dict, needle: str = "") -> None:
        with self.assertRaises(KernelError) as ctx:
            self.parse(data)
        self.assertEqual(ctx.exception.code, "REGISTRY_INVALID")
        if needle:
            self.assertIn(needle, ctx.exception.message)

    # ---------------------------------------------------------------- parsing
    def test_derives_names_and_resolves(self) -> None:
        registry = self.parse(registry_data())
        self.assertEqual(
            registry.accounts["acc_card"].account,
            "Assets:Savings:Bank:BOC:Acct-7f3a2c1d",
        )
        self.assertEqual(registry.accounts["acc_food"].account, "Expenses:Food:Dining")
        self.assertEqual(registry.resolve("中行工资卡"), ("account", "acc_card"))
        self.assertEqual(registry.resolve("acc_food"), ("account", "acc_food"))
        self.assertEqual(
            registry.resolve("Assets:Savings:Bank:BOC:Acct-7f3a2c1d"),
            ("account", "acc_card"),
        )
        self.assertEqual(registry.resolve("中国银行"), ("group", "grp_boc"))
        self.assertEqual(registry.resolve("Assets:Savings:Bank"), ("group", "grp_bank"))
        self.assertIsNone(registry.resolve("不存在"))

    def test_alias_resolution_is_normalized(self) -> None:
        registry = self.parse(registry_data())
        self.assertEqual(registry.resolve(" 中行工资卡 "), ("account", "acc_card"))

    def test_ambiguous_group_alias_is_rejected(self) -> None:
        data = registry_data()
        data["groups"].append(
            {"id": "grp_bank2", "alias": "银行", "segment": "Bank2", "parent": "grp_expenses"}
        )
        registry = self.parse(data)
        with self.assertRaises(KernelError) as ctx:
            registry.resolve("银行")
        self.assertEqual(ctx.exception.code, "REGISTRY_INVALID")
        self.assertIn("ambiguous", ctx.exception.message)

    def test_duplicate_account_alias_rejected(self) -> None:
        data = registry_data()
        data["accounts"][1]["alias"] = "中行工资卡"
        self.assert_invalid(data, "duplicate account alias")

    def test_duplicate_group_alias_under_same_parent_rejected(self) -> None:
        data = registry_data()
        data["groups"].append(
            {"id": "grp_dup", "alias": "银行", "segment": "Bank2", "parent": "grp_savings"}
        )
        self.assert_invalid(data, "duplicate group alias")

    def test_duplicate_segment_between_group_and_account_rejected(self) -> None:
        data = registry_data()
        data["groups"].append(
            {"id": "grp_dup", "alias": "重复", "segment": "Dining", "parent": "grp_food"}
        )
        self.assert_invalid(data, "duplicate segment")

    def test_unknown_parent_rejected(self) -> None:
        data = registry_data()
        data["groups"][1]["parent"] = "grp_missing"
        self.assert_invalid(data, "unknown parent")

    def test_group_cycle_rejected(self) -> None:
        data = {
            "schema_version": 2,
            "groups": [
                {"id": "grp_a", "alias": "A", "segment": "A", "parent": "grp_b"},
                {"id": "grp_b", "alias": "B", "segment": "B", "parent": "grp_a"},
            ],
            "accounts": [],
        }
        self.assert_invalid(data, "cycle")

    def test_depth_limit(self) -> None:
        data = registry_data()
        data["groups"].append(
            {"id": "grp_deep", "alias": "深", "segment": "Deep", "parent": "grp_boc"}
        )
        self.assert_invalid(data, "deeper")

    def test_root_segment_must_match_type(self) -> None:
        data = registry_data()
        data["groups"][0]["segment"] = "Asset"
        self.assert_invalid(data, "segment must equal")

    def test_opaque_segment_format(self) -> None:
        data = registry_data()
        data["accounts"][0]["segment"] = "Acct-7F3A2C1D"
        self.assert_invalid(data, "opaque segment")

    def test_semantic_segment_cannot_use_opaque_prefix(self) -> None:
        data = registry_data()
        data["accounts"][1]["segment"] = "Acct-7f3a2c1d"
        self.assert_invalid(data, "semantic segment")

    def test_schema_version_rejected(self) -> None:
        data = registry_data()
        data["schema_version"] = 1
        self.assert_invalid(data, "schema_version")

    def test_policy_is_validated_with_registry_error(self) -> None:
        data = registry_data()
        data["accounts"][0]["policy"] = {"allowed_currencies": ["cny"]}
        self.assert_invalid(data, "policy")

    # -------------------------------------------------------------- successors
    def _with_old_card(self, data: dict) -> None:
        data["accounts"].append(
            {
                "id": "acc_old",
                "alias": "旧卡",
                "segment": "Acct-aaaaaaaa",
                "mode": "opaque",
                "parent": "grp_boc",
                "status": "archived",
                "close_date": "2021-01-01",
            }
        )
        data["accounts"].append(
            {
                "id": "acc_new",
                "alias": "新卡",
                "segment": "Acct-bbbbbbbb",
                "mode": "opaque",
                "parent": "grp_boc",
                "status": "active",
                "successor_of": "acc_old",
            }
        )

    def test_successor_must_follow_archived(self) -> None:
        data = registry_data()
        self._with_old_card(data)
        self.parse(data)

    def test_successor_predecessor_must_be_archived(self) -> None:
        data = registry_data()
        self._with_old_card(data)
        data["accounts"][2]["status"] = "active"
        data["accounts"][2].pop("close_date", None)
        self.assert_invalid(data, "predecessor must be archived")

    def test_successor_cannot_reference_itself(self) -> None:
        data = registry_data()
        data["accounts"][0]["successor_of"] = "acc_card"
        self.assert_invalid(data, "itself")

    def test_successor_must_keep_root_type(self) -> None:
        data = registry_data()
        data["accounts"].append(
            {
                "id": "acc_old_expense",
                "alias": "旧支出",
                "segment": "OldExpense",
                "mode": "semantic",
                "parent": "grp_food",
                "status": "archived",
            }
        )
        data["accounts"][0]["successor_of"] = "acc_old_expense"
        self.assert_invalid(data, "same root type")

    # -------------------------------------------------------------------- plan
    def test_clean_plan_has_no_items(self) -> None:
        registry = self.parse(registry_data())
        load = self.load()
        plan = plan_registry(registry, load, revision="sha256:r")
        self.assertEqual(plan["items"], [])
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["actions"], {"open": [], "close": [], "relabel": []})

    def test_plan_missing_open(self) -> None:
        registry = self.parse(registry_data())
        load = self.load("2020-01-01 open Expenses:Food:Dining CNY\n")
        plan = plan_registry(registry, load, revision="sha256:r")
        self.assertEqual(plan["actions"]["open"], ["Assets:Savings:Bank:BOC:Acct-7f3a2c1d"])
        self.assertIn("MISSING_OPEN", plan["counts"])

    def test_plan_missing_close(self) -> None:
        data = registry_data()
        data["accounts"].append(
            {
                "id": "acc_old",
                "alias": "旧卡",
                "segment": "Acct-aaaaaaaa",
                "mode": "opaque",
                "parent": "grp_boc",
                "status": "archived",
                "close_date": "2021-01-01",
            }
        )
        registry = self.parse(data)
        load = self.load(LEDGER + "2020-01-01 open Assets:Savings:Bank:BOC:Acct-aaaaaaaa CNY\n")
        plan = plan_registry(registry, load, revision="sha256:r")
        self.assertEqual(plan["actions"]["close"], ["Assets:Savings:Bank:BOC:Acct-aaaaaaaa"])
        self.assertIn("MISSING_CLOSE", plan["counts"])

    def test_plan_alias_drift(self) -> None:
        registry = self.parse(registry_data())
        body = LEDGER.replace('alias: "中行工资卡"', 'alias: "旧名字"')
        plan = plan_registry(registry, self.load(body), revision="sha256:r")
        self.assertIn("ALIAS_DRIFT", plan["counts"])
        self.assertEqual(plan["actions"]["relabel"], ["Assets:Savings:Bank:BOC:Acct-7f3a2c1d"])

    def test_plan_unmanaged_account(self) -> None:
        registry = self.parse(registry_data())
        plan = plan_registry(
            registry, self.load(LEDGER + "2020-01-01 open Assets:Cash CNY\n"), revision="sha256:r"
        )
        self.assertIn("UNMANAGED_ACCOUNT", plan["counts"])

    def test_plan_currency_drift(self) -> None:
        data = registry_data()
        data["accounts"][0]["currencies"] = ["USD"]
        registry = self.parse(data)
        plan = plan_registry(registry, self.load(), revision="sha256:r")
        self.assertIn("CURRENCY_DRIFT", plan["counts"])

    # -------------------------------------------------------------------- view
    def test_view_reports_counts_and_warnings(self) -> None:
        registry = self.parse(registry_data())
        view = registry_view(registry, self.load(), revision="sha256:r")
        self.assertTrue(view["valid"])
        self.assertEqual(view["counts"]["accounts"], 2)
        self.assertEqual(view["counts"]["unmanaged"], 0)
        self.assertEqual(view["warnings"], [])

    def test_view_reports_invalid_registry_error(self) -> None:
        base = Path(self.tmp.name)
        (base / "accounts.yaml").write_text("schema_version: 9\n", encoding="utf-8")
        with self.assertRaises(KernelError):
            load_registry(base / "accounts.yaml")

    def test_missing_registry_is_empty(self) -> None:
        registry = load_registry(Path(self.tmp.name) / "missing.yaml")
        self.assertEqual(registry.accounts, {})
        self.assertEqual(registry_revision(Path(self.tmp.name) / "missing.yaml"), "sha256:absent")

    # --------------------------------------------------------------------- cli
    def test_cli_registry_plan_and_resolve(self) -> None:
        base = Path(self.tmp.name)
        root = base / "ledger"
        kernel = make_kernel(root)
        (root / ".ledger-kernel" / "accounts.yaml").write_text(
            text_of(registry_data()), encoding="utf-8"
        )
        kernel.close()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli_main(["--ledger", str(root), "--json", "accounts", "plan"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["schema_version"], 2)
        self.assertIn("counts", payload)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli_main(["--ledger", str(root), "accounts", "resolve", "中行工资卡"])
        self.assertEqual(code, 0)
        self.assertIn("中行工资卡", out.getvalue())
        self.assertIn("Assets:Savings:Bank:BOC:Acct-7f3a2c1d", out.getvalue())

    def test_cli_invalid_registry_returns_stable_error(self) -> None:
        base = Path(self.tmp.name)
        root = base / "ledger"
        kernel = make_kernel(root)
        (root / ".ledger-kernel" / "accounts.yaml").write_text(
            "schema_version: 2\ngroups: []\naccounts: [{id: bad}]\n", encoding="utf-8"
        )
        kernel.close()

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli_main(["--ledger", str(root), "--json", "accounts", "plan"])
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["error"]["code"], "REGISTRY_INVALID")


if __name__ == "__main__":
    unittest.main()
