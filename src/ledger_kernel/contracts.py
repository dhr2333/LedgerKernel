"""Declarative account contracts (``.ledger-kernel/contracts.yaml``).

Contracts are intentionally deterministic and declarative: no Python hooks, no
scripts. Unknown fields are rejected so a typo cannot silently weaken a policy.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import yaml
from beancount.core import account as bc_account
from beancount.core.amount import CURRENCY_RE

from .adapter import LoadResult
from .errors import CONTRACT_INVALID, KernelError
from .schema import ParsedDraft
from .util import format_amount

CADENCE_DAYS = {
    "weekly": 7,
    "monthly": 31,
    "quarterly": 92,
    "yearly": 366,
}
_CURRENCY_RE = re.compile(rf"^(?:{CURRENCY_RE})$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,63}$")


@dataclass(frozen=True)
class ReconciliationPolicy:
    cadence: str | None = None
    hard_deadline: bool = False


@dataclass(frozen=True)
class BalancePolicy:
    mode: str = "warn_only"
    min: Decimal | None = None
    max: Decimal | None = None


@dataclass(frozen=True)
class AccountPolicy:
    aliases: tuple[str, ...] = ()
    allowed_agents: tuple[str, ...] = ()
    allowed_currencies: tuple[str, ...] = ()
    allowed_tags: tuple[str, ...] = ()
    required_tags: tuple[str, ...] = ()
    reconciliation: ReconciliationPolicy = field(default_factory=ReconciliationPolicy)
    balance_policy: BalancePolicy = field(default_factory=BalancePolicy)


@dataclass
class Contract:
    accounts: dict[str, AccountPolicy]
    unmanaged_accounts: tuple[str, ...] = ()

    def policy(self, account: str) -> AccountPolicy | None:
        return self.accounts.get(account)


def _fail(message: str, **details: Any) -> None:
    raise KernelError(CONTRACT_INVALID, message, details)


def _string_list(value: Any, field: str, *, max_items: int = 50) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        _fail(f"{field} must be a list of non-empty strings")
    if len(value) > max_items:
        _fail(f"{field} has more than {max_items} items")
    if len(set(value)) != len(value):
        _fail(f"{field} contains duplicates")
    return tuple(value)


def _decimal_or_none(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        _fail(f"{field} must be a decimal value or null")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        _fail(f"{field} is not a decimal value")
    if not result.is_finite():
        _fail(f"{field} must be finite")
    return result


def _parse_policy(account: str, raw: Any) -> AccountPolicy:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _fail(f"account {account} must map to an object")
    allowed = {"aliases", "policy"}
    unknown = set(raw) - allowed
    if unknown:
        _fail(f"account {account} has unsupported fields: {', '.join(sorted(unknown))}")
    aliases = _string_list(raw.get("aliases"), f"{account}.aliases")
    for alias in aliases:
        if len(alias) > 64:
            _fail(f"{account}.aliases contains an alias longer than 64 characters")
    policy_raw = raw.get("policy") or {}
    if not isinstance(policy_raw, dict):
        _fail(f"{account}.policy must be an object")
    policy_allowed = {
        "allowed_agents",
        "allowed_currencies",
        "allowed_tags",
        "required_tags",
        "reconciliation",
        "balance_policy",
    }
    unknown = set(policy_raw) - policy_allowed
    if unknown:
        _fail(f"{account}.policy has unsupported fields: {', '.join(sorted(unknown))}")
    allowed_agents = _string_list(policy_raw.get("allowed_agents"), f"{account}.policy.allowed_agents")
    allowed_currencies = _string_list(
        policy_raw.get("allowed_currencies"), f"{account}.policy.allowed_currencies"
    )
    for currency in allowed_currencies:
        if not _CURRENCY_RE.match(currency):
            _fail(f"{account}.policy.allowed_currencies has invalid token: {currency!r}")
    allowed_tags = _string_list(policy_raw.get("allowed_tags"), f"{account}.policy.allowed_tags")
    required_tags = _string_list(policy_raw.get("required_tags"), f"{account}.policy.required_tags")
    for tag in (*allowed_tags, *required_tags):
        if not _TAG_RE.match(tag):
            _fail(f"{account}.policy has invalid tag: {tag!r}")
    if required_tags and allowed_tags and not set(required_tags) <= set(allowed_tags):
        _fail(f"{account}.policy.required_tags must be a subset of allowed_tags")

    reconciliation_raw = policy_raw.get("reconciliation") or {}
    if not isinstance(reconciliation_raw, dict):
        _fail(f"{account}.policy.reconciliation must be an object")
    unknown = set(reconciliation_raw) - {"cadence", "hard_deadline"}
    if unknown:
        _fail(f"{account}.policy.reconciliation has unsupported fields: {', '.join(sorted(unknown))}")
    cadence = reconciliation_raw.get("cadence")
    if cadence is not None and cadence not in CADENCE_DAYS:
        _fail(f"{account}.policy.reconciliation.cadence is invalid: {cadence!r}")
    hard_deadline = reconciliation_raw.get("hard_deadline", False)
    if not isinstance(hard_deadline, bool):
        _fail(f"{account}.policy.reconciliation.hard_deadline must be a boolean")

    balance_raw = policy_raw.get("balance_policy") or {}
    if not isinstance(balance_raw, dict):
        _fail(f"{account}.policy.balance_policy must be an object")
    unknown = set(balance_raw) - {"mode", "min", "max"}
    if unknown:
        _fail(f"{account}.policy.balance_policy has unsupported fields: {', '.join(sorted(unknown))}")
    mode = balance_raw.get("mode", "warn_only")
    if mode != "warn_only":
        _fail(f"{account}.policy.balance_policy.mode only supports warn_only in v0.1")
    minimum = _decimal_or_none(balance_raw.get("min"), f"{account}.policy.balance_policy.min")
    maximum = _decimal_or_none(balance_raw.get("max"), f"{account}.policy.balance_policy.max")
    if minimum is not None and maximum is not None and minimum > maximum:
        _fail(f"{account}.policy.balance_policy.min is greater than max")

    return AccountPolicy(
        aliases=aliases,
        allowed_agents=allowed_agents,
        allowed_currencies=allowed_currencies,
        allowed_tags=allowed_tags,
        required_tags=required_tags,
        reconciliation=ReconciliationPolicy(cadence=cadence, hard_deadline=hard_deadline),
        balance_policy=BalancePolicy(mode=mode, min=minimum, max=maximum),
    )


def parse_contract(raw: Any) -> Contract:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _fail("contracts.yaml must contain an object")
    allowed = {"schema_version", "accounts", "unmanaged_accounts"}
    unknown = set(raw) - allowed
    if unknown:
        _fail(f"contracts.yaml has unsupported fields: {', '.join(sorted(unknown))}")
    version = raw.get("schema_version", 1)
    if version != 1:
        _fail(f"unsupported contracts schema_version: {version!r}")
    accounts_raw = raw.get("accounts") or {}
    if not isinstance(accounts_raw, dict):
        _fail("accounts must be an object keyed by account name")
    accounts: dict[str, AccountPolicy] = {}
    alias_owner: dict[str, str] = {}
    for name, value in accounts_raw.items():
        if not isinstance(name, str) or not bc_account.is_valid(name):
            _fail(f"invalid account name in contracts: {name!r}")
        policy = _parse_policy(name, value)
        for alias in policy.aliases:
            if alias in alias_owner and alias_owner[alias] != name:
                _fail(f"alias {alias!r} is assigned to both {alias_owner[alias]} and {name}")
            alias_owner[alias] = name
        accounts[name] = policy

    unmanaged = _string_list(raw.get("unmanaged_accounts"), "unmanaged_accounts", max_items=500)
    for name in unmanaged:
        if not bc_account.is_valid(name):
            _fail(f"invalid account name in unmanaged_accounts: {name!r}")
    overlap = sorted(set(unmanaged) & set(accounts))
    if overlap:
        _fail(
            f"unmanaged_accounts must not overlap accounts: {', '.join(overlap)}",
            overlap=overlap,
        )
    return Contract(accounts=accounts, unmanaged_accounts=unmanaged)


def parse_contract_text(text: str) -> Contract:
    """Strictly parse candidate contracts text without touching the disk."""
    if not isinstance(text, str) or not text.strip():
        _fail("contracts.yaml must contain a YAML object")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise KernelError(CONTRACT_INVALID, f"cannot parse contracts.yaml: {exc}") from exc
    return parse_contract(raw)


def load_contract(path: Path) -> Contract:
    if not path.exists():
        return Contract(accounts={})
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KernelError(CONTRACT_INVALID, f"cannot read contracts.yaml: {exc}") from exc
    return parse_contract_text(text)


def default_contract_yaml() -> str:
    return (
        "# LedgerKernel account contracts (schema_version 1).\n"
        "# Keep this file declarative and versioned with your ledger.\n"
        "# unmanaged_accounts: accounts you know about but do not manage yet.\n"
        "schema_version: 1\n"
        "unmanaged_accounts: []\n"
        "accounts: {}\n"
    )


def contract_coverage(
    contract: Contract, load: LoadResult, *, today: dt.date | None = None
) -> dict[str, Any]:
    """Coverage of *active* ledger accounts by contracts.

    An account is active when it is open today (opened, not closed). Contract
    entries for accounts that are absent or inactive in the ledger are reported
    separately; declaring them is allowed so users can prepare for new accounts.
    """
    today = today or dt.date.today()
    ledger_accounts = set(load.accounts)
    active = sorted(name for name, info in load.accounts.items() if info.is_open_on(today))
    active_set = set(active)
    declared_unmanaged = set(contract.unmanaged_accounts)
    managed = [name for name in active if name in contract.accounts]
    unmanaged = [name for name in active if name in declared_unmanaged]
    uncovered = [
        name for name in active if name not in contract.accounts and name not in declared_unmanaged
    ]
    unopened = [name for name in sorted(contract.accounts) if name not in ledger_accounts]
    inactive = [
        name for name in sorted(contract.accounts) if name in ledger_accounts and name not in active_set
    ]
    return {
        "active_account_count": len(active),
        "managed_accounts": managed,
        "unmanaged_accounts": unmanaged,
        "uncovered_accounts": uncovered,
        "unopened_contract_accounts": unopened,
        "inactive_contract_accounts": inactive,
        "ratio": None if not active else round(len(managed) / len(active), 4),
    }


def contract_warnings(coverage: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Non-blocking warnings for a parsed contract, derived from coverage."""
    return [
        {
            "code": "CONTRACT_UNKNOWN_ACCOUNT",
            "severity": "warning",
            "account": name,
            "message": f"contract references an account not opened in the ledger: {name}",
        }
        for name in coverage.get("unopened_contract_accounts", [])
    ]


def _check(
    checks: list[dict[str, Any]],
    code: str,
    severity: str,
    message: str,
    *,
    account: str | None = None,
) -> None:
    checks.append({"code": code, "severity": severity, "message": message, "account": account})


def evaluate_contract(
    contract: Contract,
    parsed: ParsedDraft,
    *,
    agent_id: str,
    load: LoadResult,
    balances_after: Mapping[tuple[str, str], Decimal],
    last_posting_dates: Mapping[str, dt.date],
) -> list[dict[str, Any]]:
    """Run deterministic contract checks. Returns check records."""
    checks: list[dict[str, Any]] = []
    known_accounts: set[str] = set(load.accounts)
    for name in contract.accounts:
        if name not in known_accounts:
            _check(
                checks,
                "CONTRACT_UNKNOWN_ACCOUNT",
                "warning",
                f"contract references an account not opened in the ledger: {name}",
                account=name,
            )

    for transaction in parsed.transactions:
        tags = set(transaction.tags)
        for posting in transaction.postings:
            policy = contract.policy(posting.account)
            if policy is None:
                continue
            if policy.allowed_agents and agent_id not in policy.allowed_agents:
                _check(
                    checks,
                    "CONTRACT_AGENT",
                    "block",
                    f"agent {agent_id!r} is not allowed to propose postings on {posting.account}",
                    account=posting.account,
                )
            if policy.allowed_currencies and posting.currency not in policy.allowed_currencies:
                _check(
                    checks,
                    "CONTRACT_CURRENCY",
                    "block",
                    f"{posting.account} only allows currencies: {', '.join(policy.allowed_currencies)}",
                    account=posting.account,
                )
            if policy.allowed_tags:
                extra = sorted(tags - set(policy.allowed_tags))
                if extra:
                    _check(
                        checks,
                        "CONTRACT_TAG",
                        "block",
                        f"{posting.account} does not allow tags: {', '.join(extra)}",
                        account=posting.account,
                    )
            if policy.required_tags:
                missing = sorted(set(policy.required_tags) - tags)
                if missing:
                    _check(
                        checks,
                        "CONTRACT_TAG_REQUIRED",
                        "block",
                        f"{posting.account} requires tags: {', '.join(missing)}",
                        account=posting.account,
                    )
            cadence = policy.reconciliation.cadence
            if cadence:
                last = last_posting_dates.get(posting.account)
                if last is not None and (transaction.date - last).days > CADENCE_DAYS[cadence]:
                    _check(
                        checks,
                        "CONTRACT_RECONCILIATION",
                        "warning",
                        f"{posting.account} was last posted {last.isoformat()}, beyond the {cadence} reconciliation cadence",
                        account=posting.account,
                    )
            balance_policy = policy.balance_policy
            if balance_policy.min is not None or balance_policy.max is not None:
                for (name, currency), amount in balances_after.items():
                    if name != posting.account:
                        continue
                    if balance_policy.min is not None and amount < balance_policy.min:
                        _check(
                            checks,
                            "CONTRACT_BALANCE",
                            "warning",
                            f"{name} balance {format_amount(amount)} {currency} is below the suggested minimum "
                            f"{format_amount(balance_policy.min)}",
                            account=name,
                        )
                    if balance_policy.max is not None and amount > balance_policy.max:
                        _check(
                            checks,
                            "CONTRACT_BALANCE",
                            "warning",
                            f"{name} balance {format_amount(amount)} {currency} is above the suggested maximum "
                            f"{format_amount(balance_policy.max)}",
                            account=name,
                        )
    return checks


def accounts_with_aliases(contract: Contract) -> dict[str, list[str]]:
    return {name: list(policy.aliases) for name, policy in contract.accounts.items() if policy.aliases}


def account_contract_view(policy: AccountPolicy | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    return {
        "aliases": list(policy.aliases),
        "allowed_agents": list(policy.allowed_agents),
        "allowed_currencies": list(policy.allowed_currencies),
        "allowed_tags": list(policy.allowed_tags),
        "required_tags": list(policy.required_tags),
        "reconciliation": {
            "cadence": policy.reconciliation.cadence,
            "hard_deadline": policy.reconciliation.hard_deadline,
        },
        "balance_policy": {
            "mode": policy.balance_policy.mode,
            "min": None if policy.balance_policy.min is None else format_amount(policy.balance_policy.min),
            "max": None if policy.balance_policy.max is None else format_amount(policy.balance_policy.max),
        },
    }
