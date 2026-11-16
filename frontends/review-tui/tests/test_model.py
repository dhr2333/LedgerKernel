from __future__ import annotations

import unittest

from ledger_review_tui import model

from .helpers import BY_ACCOUNT

DETAIL = {
    "draft_id": "drf_1",
    "state": "NeedsHumanApproval",
    "validation_status": "pass_with_warnings",
    "checks": [
        {
            "severity": "warning",
            "code": "CONTRACT_CURRENCY",
            "message": "currency not allowed",
        },
        {"severity": "block", "code": "SCHEMA_INVALID", "message": "bad amount"},
    ],
    "fragment": """
2026-01-02 * "Books" "Lunch"
  Expenses:Books  120.00 CNY
  Assets:Bank  -120.00 CNY
""",
    "diff": {
        "changed_files": ["transactions/kernel-import.bean"],
        "deltas": [
            {
                "account": "Assets:Bank",
                "before": "1000.00",
                "after": "880.00",
                "delta": "-120.00",
                "currency": "CNY",
            }
        ],
        "file_diff": """--- a/transactions/kernel-import.bean
+++ b/transactions/kernel-import.bean
+2026-01-02 * "Books" "Lunch"
""",
    },
}


class RowTests(unittest.TestCase):
    def test_build_rows_flattens_groups_and_drafts(self) -> None:
        rows = model.build_rows(BY_ACCOUNT)
        self.assertEqual([row.kind for row in rows], ["group", "draft", "group"])
        self.assertIn("Assets:Bank", rows[0].label)
        self.assertIn("reconciliation current", rows[0].label)
        self.assertEqual(rows[1].draft_id, "drf_1")
        self.assertIn("Books", rows[1].label)
        self.assertIn("120.00 CNY", rows[1].label)
        self.assertIn("Expenses:Books", rows[2].label)

    def test_selected_draft_id_ignores_group_headers(self) -> None:
        rows = model.build_rows(BY_ACCOUNT)
        self.assertIsNone(model.selected_draft_id(rows, 0))
        self.assertEqual(model.selected_draft_id(rows, 1), "drf_1")
        self.assertIsNone(model.selected_draft_id(rows, 2))
        self.assertIsNone(model.selected_draft_id(rows, 99))
        self.assertIsNone(model.selected_draft_id([], 0))

    def test_move_selection_clamps_to_bounds(self) -> None:
        rows = model.build_rows(BY_ACCOUNT)
        self.assertEqual(model.move_selection(rows, 0, -1), 0)
        self.assertEqual(model.move_selection(rows, 0, 1), 1)
        self.assertEqual(model.move_selection(rows, 2, 5), 2)
        self.assertEqual(model.move_selection([], 4, 1), 0)


class ConfirmStringTests(unittest.TestCase):
    def test_confirm_string_matches_cli_comma_join(self) -> None:
        self.assertEqual(model.confirm_string([]), "")
        self.assertEqual(model.confirm_string(["drf_1"]), "drf_1")
        self.assertEqual(
            model.confirm_string(["drf_1", "drf_2"]), "drf_1,drf_2"
        )


class DetailRenderingTests(unittest.TestCase):
    def test_detail_lines_include_fragment_checks_and_diff(self) -> None:
        text = "\n".join(model.detail_lines(DETAIL))
        self.assertIn("draft drf_1", text)
        self.assertIn("validation=pass_with_warnings", text)
        self.assertIn("Expenses:Books  120.00 CNY", text)
        self.assertIn("[warning] CONTRACT_CURRENCY: currency not allowed", text)
        self.assertIn("[block] SCHEMA_INVALID: bad amount", text)
        self.assertIn("files: transactions/kernel-import.bean", text)
        self.assertIn("balance: Assets:Bank  1000.00 -> 880.00", text)
        self.assertIn("-120.00 CNY", text)
        self.assertIn("file diff:", text)
        self.assertIn("+2026-01-02", text)

    def test_detail_lines_without_validation_says_so(self) -> None:
        text = "\n".join(
            model.detail_lines({"draft_id": "drf_2", "state": "Submitted"})
        )
        self.assertIn("(not validated yet)", text)
        self.assertIn("no blocking or warning checks", text)
        self.assertIn("(no diff recorded)", text)

    def test_diff_lines_cap_rendered_file_diff(self) -> None:
        diff = {"file_diff": "\n".join(f"line {index}" for index in range(60))}
        lines = model.diff_lines(diff)
        rendered = [line for line in lines if line.startswith("  line ")]
        self.assertEqual(len(rendered), 40)

    def test_checks_lines_summarise_severity_code_and_message(self) -> None:
        lines = model.checks_lines(DETAIL["checks"])
        self.assertIn("[warning] CONTRACT_CURRENCY: currency not allowed", lines)
        self.assertIn("[block] SCHEMA_INVALID: bad amount", lines)
        self.assertEqual(model.checks_lines([]), ["no blocking or warning checks"])


class ListRenderingTests(unittest.TestCase):
    def test_render_list_shows_groups_drafts_warnings(self) -> None:
        text = model.render_list(BY_ACCOUNT)
        self.assertIn("== Assets:Bank  open  1000.00 CNY", text)
        self.assertIn("reconciliation current", text)
        self.assertIn("drf_1  2026-01-02  120.00 CNY  Books", text)
        self.assertIn("[NeedsHumanApproval / pass]", text)
        self.assertIn("warning: CONTRACT_CURRENCY", text)
        self.assertIn("== Expenses:Books", text)
        self.assertIn("pending: none", text)

    def test_render_list_empty_payload_uses_note(self) -> None:
        self.assertEqual(model.render_list({"groups": []}), "no pending drafts")
        self.assertEqual(
            model.render_list(
                {"groups": [], "note": "unknown account: Assets:Nope"}
            ),
            "unknown account: Assets:Nope",
        )


if __name__ == "__main__":
    unittest.main()
