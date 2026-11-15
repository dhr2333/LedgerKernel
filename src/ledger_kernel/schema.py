"""Structured draft schema, text safety rules and Beancount rendering.

Agents never submit raw Beancount text. The kernel accepts a narrow JSON schema
and renders the only bytes that may ever be appended. This is what makes the
ADR-012 delta assertion and fragment fingerprint meaningful.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

from beancount.core import account as bc_account
from beancount.core.amount import CURRENCY_RE

from .errors import SCHEMA_INVALID, UNSAFE_TEXT, KernelError
from .util import format_amount, parse_amount, parse_datetime

SCHEMA_VERSION = 1
MAX_TRANSACTIONS = 20
MAX_POSTINGS = 50
MAX_PAYEE = 160
MAX_NARRATION = 600
MAX_RATIONALE = 1000
MAX_TEXT_EVIDENCE = 2000
MAX_TAGS = 20
MAX_EVIDENCE = 20

_ALLOWED_FLAGS = {"*", "!"}
_ALLOWED_ROOTS = {"Assets", "Liabilities", "Equity", "Income", "Expenses"}
_ACCOUNT_COMPONENT_RE = re.compile(r"^[A-Z][A-Za-z0-9-]*$")
_CURRENCY_FULL_RE = re.compile(rf"^(?:{CURRENCY_RE})$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,63}$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CONTROL_MESSAGE = "contains control or direction-override characters"


@dataclass(frozen=True)
class ParsedPosting:
    account: str
    units: Decimal
    currency: str


@dataclass(frozen=True)
class ParsedTransaction:
    date: dt.date
    flag: str
    payee: str | None
    narration: str
    tags: tuple[str, ...]
    links: tuple[str, ...]
    postings: tuple[ParsedPosting, ...]


@dataclass(frozen=True)
class ParsedEvidence:
    kind: str
    locator: str
    sha256: str | None = None
    page_or_region: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class ParsedDraft:
    transactions: tuple[ParsedTransaction, ...]
    evidence: tuple[ParsedEvidence, ...]
    confidence: float | None
    rationale_summary: str | None
    expires_at: dt.datetime | None
    supersedes: str | None
    account_refs: tuple[str, ...]

    @property
    def accounts(self) -> tuple[str, ...]:
        seen: set[str] = set()
        for txn in self.transactions:
            for posting in txn.postings:
                seen.add(posting.account)
        return tuple(sorted(seen))


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise KernelError(SCHEMA_INVALID, f"{field} must be an object")
    return value


def _check_text(value: Any, field: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise KernelError(SCHEMA_INVALID, f"{field} must be a string")
    if not value and not allow_empty:
        raise KernelError(SCHEMA_INVALID, f"{field} must not be empty")
    if len(value) > limit:
        raise KernelError(SCHEMA_INVALID, f"{field} exceeds {limit} characters")
    for char in value:
        code = ord(char)
        if (
            code < 0x20
            or 0x7F <= code <= 0x9F
            or code in (0x2028, 0x2029, 0xFEFF)
            or 0x202A <= code <= 0x202E
            or 0x2066 <= code <= 0x2069
            or 0xD800 <= code <= 0xDFFF
        ):
            raise KernelError(
                UNSAFE_TEXT,
                f"{field} {_CONTROL_MESSAGE}",
                {"field": field, "codepoint": f"U+{code:04X}"},
            )
    return value


def _check_display_text(value: Any, field: str, limit: int) -> str:
    """Rationale/evidence text may contain newlines but no other controls."""
    if not isinstance(value, str):
        raise KernelError(SCHEMA_INVALID, f"{field} must be a string")
    if len(value) > limit:
        raise KernelError(SCHEMA_INVALID, f"{field} exceeds {limit} characters")
    for char in value:
        code = ord(char)
        if char in ("\n", "\t"):
            continue
        if code < 0x20 or 0x7F <= code <= 0x9F or 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069:
            raise KernelError(UNSAFE_TEXT, f"{field} {_CONTROL_MESSAGE}", {"field": field})
    return value


def _check_account(value: Any, field: str) -> str:
    if not isinstance(value, str) or not bc_account.is_valid(value):
        raise KernelError(SCHEMA_INVALID, f"{field} is not a valid Beancount account name")
    root = value.split(":", 1)[0]
    if root not in _ALLOWED_ROOTS:
        raise KernelError(SCHEMA_INVALID, f"{field} has an unknown root account: {root}")
    for component in value.split(":")[1:]:
        if not _ACCOUNT_COMPONENT_RE.match(component):
            raise KernelError(SCHEMA_INVALID, f"{field} has an invalid component: {component}")
    return value


def _check_currency(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _CURRENCY_FULL_RE.match(value):
        raise KernelError(SCHEMA_INVALID, f"{field} is not a valid currency token")
    return value


def _check_tag(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _TAG_RE.match(value):
        raise KernelError(SCHEMA_INVALID, f"{field} must be a plain tag/link name")
    return value


def _check_text_list(value: Any, field: str, limit: int, max_items: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise KernelError(SCHEMA_INVALID, f"{field} must be a list")
    if len(value) > max_items:
        raise KernelError(SCHEMA_INVALID, f"{field} has more than {max_items} items")
    items = [_check_tag(item, f"{field}[{index}]") for index, item in enumerate(value)]
    if len(set(items)) != len(items):
        raise KernelError(SCHEMA_INVALID, f"{field} contains duplicates")
    return tuple(items)


def _parse_posting(raw: Any, index: int) -> ParsedPosting:
    posting = _require_mapping(raw, f"transactions.postings[{index}]")
    allowed = {"account", "units", "currency"}
    unknown = set(posting) - allowed
    if unknown:
        raise KernelError(
            SCHEMA_INVALID,
            f"posting has unsupported fields: {', '.join(sorted(unknown))}",
            {"unsupported": sorted(unknown)},
        )
    for key in ("account", "units", "currency"):
        if key not in posting:
            raise KernelError(SCHEMA_INVALID, f"posting is missing {key}")
    account = _check_account(posting["account"], "posting.account")
    units = parse_amount(posting["units"], "posting.units")
    currency = _check_currency(posting["currency"], "posting.currency")
    return ParsedPosting(account=account, units=units, currency=currency)


def _parse_transaction(raw: Any, index: int) -> ParsedTransaction:
    txn = _require_mapping(raw, f"transactions[{index}]")
    allowed = {"date", "flag", "payee", "narration", "tags", "links", "postings"}
    unknown = set(txn) - allowed
    if unknown:
        raise KernelError(
            SCHEMA_INVALID,
            f"transaction has unsupported fields: {', '.join(sorted(unknown))}",
            {"unsupported": sorted(unknown)},
        )
    if "date" not in txn or "narration" not in txn or "postings" not in txn:
        raise KernelError(SCHEMA_INVALID, "transaction requires date, narration and postings")
    date_raw = txn["date"]
    if not isinstance(date_raw, str):
        raise KernelError(SCHEMA_INVALID, "transaction.date must be an ISO date string")
    try:
        date = dt.date.fromisoformat(date_raw)
    except ValueError as exc:
        raise KernelError(SCHEMA_INVALID, f"transaction.date is invalid: {date_raw!r}") from exc
    flag = txn.get("flag", "*")
    if flag not in _ALLOWED_FLAGS:
        raise KernelError(SCHEMA_INVALID, "transaction.flag must be '*' or '!'")
    payee_raw = txn.get("payee")
    payee = None
    if payee_raw is not None:
        payee = _check_text(payee_raw, "transaction.payee", MAX_PAYEE, allow_empty=True)
        if not payee.strip():
            payee = None
    narration = _check_text(txn["narration"], "transaction.narration", MAX_NARRATION)
    tags = _check_text_list(txn.get("tags"), "transaction.tags", 64, MAX_TAGS)
    links = _check_text_list(txn.get("links"), "transaction.links", 64, MAX_TAGS)
    postings_raw = txn["postings"]
    if not isinstance(postings_raw, list):
        raise KernelError(SCHEMA_INVALID, "transaction.postings must be a list")
    if not 2 <= len(postings_raw) <= MAX_POSTINGS:
        raise KernelError(SCHEMA_INVALID, f"transaction.postings must contain 2..{MAX_POSTINGS} postings")
    postings = tuple(_parse_posting(item, pos) for pos, item in enumerate(postings_raw))
    return ParsedTransaction(
        date=date,
        flag=flag,
        payee=payee,
        narration=narration,
        tags=tags,
        links=links,
        postings=postings,
    )


def _parse_evidence(raw: Any, index: int) -> ParsedEvidence:
    evidence = _require_mapping(raw, f"evidence[{index}]")
    allowed = {"kind", "locator", "sha256", "page_or_region", "note"}
    unknown = set(evidence) - allowed
    if unknown:
        raise KernelError(SCHEMA_INVALID, f"evidence has unsupported fields: {', '.join(sorted(unknown))}")
    kind = evidence.get("kind")
    if kind not in {"file", "text", "external"}:
        raise KernelError(SCHEMA_INVALID, "evidence.kind must be file, text or external")
    locator = _check_text(evidence.get("locator"), "evidence.locator", 500)
    sha = evidence.get("sha256")
    if sha is not None:
        if not isinstance(sha, str) or not _HEX64_RE.match(sha):
            raise KernelError(SCHEMA_INVALID, "evidence.sha256 must be 64 hex characters")
        sha = sha.lower()
    page = evidence.get("page_or_region")
    if page is not None:
        page = _check_display_text(page, "evidence.page_or_region", 120)
    note = evidence.get("note")
    if note is not None:
        note = _check_display_text(note, "evidence.note", 300)
    return ParsedEvidence(kind=kind, locator=locator, sha256=sha, page_or_region=page, note=note)


def parse_payload(
    payload: Any,
    *,
    ledger_id: str,
    agent_id: str,
    now: dt.datetime | None = None,
    allow_past_expiry: bool = False,
) -> ParsedDraft:
    """Validate an untrusted proposal and return the parsed, immutable spec."""
    if not isinstance(payload, dict):
        raise KernelError(SCHEMA_INVALID, "draft payload must be a JSON object")
    allowed_top = {
        "schema_version",
        "draft_id",
        "created_at",
        "expires_at",
        "submitted_by",
        "ledger_id",
        "base_revision",
        "contract_revision",
        "operation",
        "transactions",
        "evidence",
        "account_refs",
        "confidence",
        "rationale_summary",
        "supersedes",
    }
    unknown = set(payload) - allowed_top
    if unknown:
        raise KernelError(SCHEMA_INVALID, f"draft has unsupported fields: {', '.join(sorted(unknown))}")
    version = payload.get("schema_version", SCHEMA_VERSION)
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise KernelError(SCHEMA_INVALID, f"unsupported draft schema_version: {version!r}")
    if payload.get("operation", "append_transactions") != "append_transactions":
        raise KernelError(SCHEMA_INVALID, "v0.1 only supports operation=append_transactions")
    if payload.get("ledger_id") not in (None, ledger_id):
        raise KernelError(SCHEMA_INVALID, "draft binds a different ledger_id")
    submitted_by = payload.get("submitted_by")
    if submitted_by is not None:
        if not isinstance(submitted_by, dict):
            raise KernelError(SCHEMA_INVALID, "submitted_by must be an object")
        if submitted_by.get("agent_id") != agent_id:
            raise KernelError(SCHEMA_INVALID, "submitted_by.agent_id does not match the submitting identity")
        if "agent_version" in submitted_by and not isinstance(submitted_by["agent_version"], str):
            raise KernelError(SCHEMA_INVALID, "submitted_by.agent_version must be a string")
    transactions_raw = payload.get("transactions")
    if not isinstance(transactions_raw, list) or not transactions_raw:
        raise KernelError(SCHEMA_INVALID, "transactions must be a non-empty list")
    if len(transactions_raw) > MAX_TRANSACTIONS:
        raise KernelError(SCHEMA_INVALID, f"at most {MAX_TRANSACTIONS} transactions per draft")
    transactions = tuple(_parse_transaction(item, index) for index, item in enumerate(transactions_raw))
    evidence_raw = payload.get("evidence") or []
    if not isinstance(evidence_raw, list):
        raise KernelError(SCHEMA_INVALID, "evidence must be a list")
    if len(evidence_raw) > MAX_EVIDENCE:
        raise KernelError(SCHEMA_INVALID, f"at most {MAX_EVIDENCE} evidence items")
    evidence = tuple(_parse_evidence(item, index) for index, item in enumerate(evidence_raw))
    confidence = payload.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise KernelError(SCHEMA_INVALID, "confidence must be a number")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise KernelError(SCHEMA_INVALID, "confidence must be between 0 and 1")
    rationale = payload.get("rationale_summary")
    if rationale is not None:
        rationale = _check_display_text(rationale, "rationale_summary", MAX_RATIONALE)
    expires_at = None
    if payload.get("expires_at") is not None:
        if not isinstance(payload["expires_at"], str):
            raise KernelError(SCHEMA_INVALID, "expires_at must be an ISO timestamp")
        try:
            expires_at = parse_datetime(payload["expires_at"])
        except ValueError as exc:
            raise KernelError(SCHEMA_INVALID, "expires_at is not a valid timestamp") from exc
        reference = now or dt.datetime.now(tz=dt.timezone.utc)
        if expires_at <= reference and not allow_past_expiry:
            raise KernelError(SCHEMA_INVALID, "expires_at must be in the future")
    supersedes = payload.get("supersedes")
    if supersedes is not None:
        supersedes = _check_text(supersedes, "supersedes", 80)
    parsed = ParsedDraft(
        transactions=transactions,
        evidence=evidence,
        confidence=confidence,
        rationale_summary=rationale,
        expires_at=expires_at,
        supersedes=supersedes,
        account_refs=(),
    )
    account_refs = payload.get("account_refs")
    derived = parsed.accounts
    if account_refs is not None:
        if not isinstance(account_refs, list) or not all(isinstance(item, str) for item in account_refs):
            raise KernelError(SCHEMA_INVALID, "account_refs must be a list of account names")
        if set(account_refs) != set(derived):
            raise KernelError(
                SCHEMA_INVALID,
                "account_refs must list exactly the accounts used by the transactions",
                {"declared": sorted(set(account_refs)), "derived": list(derived)},
            )
    return ParsedDraft(
        transactions=parsed.transactions,
        evidence=parsed.evidence,
        confidence=parsed.confidence,
        rationale_summary=parsed.rationale_summary,
        expires_at=parsed.expires_at,
        supersedes=parsed.supersedes,
        account_refs=derived,
    )


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def render_transaction(transaction: ParsedTransaction, newline: str = "\n") -> str:
    header = f"{transaction.date.isoformat()} {transaction.flag}"
    if transaction.payee:
        header += f' "{_quote(transaction.payee)}"'
    header += f' "{_quote(transaction.narration)}"'
    for tag in transaction.tags:
        header += f" #{tag}"
    for link in transaction.links:
        header += f" ^{link}"
    lines = [header]
    for posting in transaction.postings:
        lines.append(f"  {posting.account}  {format_amount(posting.units)} {posting.currency}")
    return newline.join(lines) + newline


def render_fragment(transactions: Sequence[ParsedTransaction], newline: str = "\n") -> str:
    if not transactions:
        raise KernelError(SCHEMA_INVALID, "cannot render an empty transaction fragment")
    return "".join(render_transaction(transaction, newline) for transaction in transactions)


def canonical_transaction(transaction: ParsedTransaction) -> tuple:
    """Canonical shape used by the ADR-012 delta assertion.

    Must stay in sync with ``adapter.canonical_entry``: metadata is excluded,
    tags/links are sorted, and amounts compare as Decimals.
    """
    return (
        "Transaction",
        transaction.date,
        transaction.flag,
        transaction.payee,
        transaction.narration,
        tuple(sorted(transaction.tags)),
        tuple(sorted(transaction.links)),
        tuple(
            (
                "Posting",
                posting.account,
                ("Amount", posting.units, posting.currency),
                None,
                None,
                None,
            )
            for posting in transaction.postings
        ),
    )


def display_accounts(payload: Any) -> list[str]:
    """Best-effort extraction for CLI display before a draft is parsed."""
    result: set[str] = set()
    if isinstance(payload, dict):
        transactions = payload.get("transactions")
        if isinstance(transactions, list):
            for transaction in transactions:
                if not isinstance(transaction, dict):
                    continue
                for posting in transaction.get("postings") or []:
                    if isinstance(posting, dict) and isinstance(posting.get("account"), str):
                        result.add(posting["account"])
    return sorted(result)


def iter_postings(transactions: Iterable[ParsedTransaction]) -> Iterable[tuple[dt.date, ParsedTransaction, ParsedPosting]]:
    for transaction in transactions:
        for posting in transaction.postings:
            yield transaction.date, transaction, posting
