from __future__ import annotations

import unittest
from decimal import Decimal

from ledger_kernel.errors import KernelError
from ledger_kernel.schema import canonical_transaction, parse_payload, render_fragment


def raw(**overrides):
    body = {
        "schema_version": 1,
        "transactions": [
            {
                "date": "2026-01-02",
                "payee": "Shop",
                "narration": "Lunch",
                "postings": [
                    {"account": "Expenses:Food", "units": "25.00", "currency": "CNY"},
                    {"account": "Assets:Cash", "units": "-25.00", "currency": "CNY"},
                ],
            }
        ],
    }
    body.update(overrides)
    return body


class SchemaTests(unittest.TestCase):
    def test_newline_injection_is_blocked(self) -> None:
        body = raw()
        body["transactions"][0]["narration"] = 'Lunch"\n2026-01-03 open Assets:Injected CNY\n"'
        with self.assertRaises(KernelError) as caught:
            parse_payload(body, ledger_id="lk", agent_id="a")
        self.assertEqual(caught.exception.code, "UNSAFE_TEXT")

    def test_quotes_and_backslashes_round_trip(self) -> None:
        body = raw()
        body["transactions"][0]["payee"] = 'A "Book" \\ Shop'
        parsed = parse_payload(body, ledger_id="lk", agent_id="a")
        fragment = render_fragment(parsed.transactions)
        self.assertIn('\\"Book\\"', fragment)
        self.assertIn("\\\\ Shop", fragment)

        from beancount.parser import parser

        entries, errors, _ = parser.parse_string(fragment)
        self.assertEqual(errors, [])
        self.assertEqual(entries[0].payee, 'A "Book" \\ Shop')

    def test_float_amount_is_rejected(self) -> None:
        body = raw()
        body["transactions"][0]["postings"][0]["units"] = 25.0
        with self.assertRaises(KernelError) as caught:
            parse_payload(body, ledger_id="lk", agent_id="a")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_unknown_posting_shape_is_rejected(self) -> None:
        body = raw()
        body["transactions"][0]["postings"][0]["price"] = "1 EUR"
        with self.assertRaises(KernelError) as caught:
            parse_payload(body, ledger_id="lk", agent_id="a")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_canonical_transaction_excludes_meta(self) -> None:
        parsed = parse_payload(raw(), ledger_id="lk", agent_id="a")
        canonical = canonical_transaction(parsed.transactions[0])
        self.assertEqual(canonical[0], "Transaction")
        self.assertNotIn("meta", canonical)


if __name__ == "__main__":
    unittest.main()
