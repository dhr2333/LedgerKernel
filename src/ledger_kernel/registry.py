"""Account registry schema v2 (``.ledger-kernel/accounts.yaml``, draft).

The registry is the *model-domain* source of truth: it owns aliases, the
account tree, lifecycle and policies. The ledger remains the append-only
*factual* source of truth. This module only parses, validates, resolves and
plans; it never writes the ledger.

Schema v2 is a pre-release breaking change (``AGENTS.md`` allows it before the
first stable release). v1 ``contracts.yaml`` is intentionally not read here.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml
from beancount.core import account as bc_account
from beancount.core.amount import CURRENCY_RE

from .adapter import LoadResult
from .contracts import (
    AccountPolicy,
    account_contract_view,
    parse_contract,
)
from .errors import REGISTRY_INVALID, KernelError
from .util import sha256_file

SCHEMA_VERSION = 2
TYPES = ("Assets", "Liabilities", "Equity", "Income", "Expenses")
MAX_GROUP_DEPTH = 3
SEGMENT_RE = re.compile(r"^[A-Z][A-Za-z0-9-]{0,63}$")
OPAQUE_RE = re.compile(r"^Acct-[0-9a-f]{8,12}$")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_CURRENCY_RE = re.compile(rf"^(?:{CURRENCY_RE})$")
_BOOKINGS = {"STRICT", "NONE", "AVERAGE", "FIFO", "LIFO", "HIFO"}
_STATUSES = {"active", "archived"}
_MODES = {"opaque", "semantic"}


@dataclass(frozen=True)
class RegistryGroup:
    id: str
    alias: str
    segment: str
    parent: str | None
    type: str
    path: str


@dataclass(frozen=True)
class RegistryAccount:
    id: str
    alias: str
    segment: str
    mode: str
    parent: str
    status: str
    successor_of: str | None
    policy: AccountPolicy
    account: str
    type: str
    open_date: dt.date | None = None
    close_date: dt.date | None = None
    currencies: tuple[str, ...] = ()
    booking: str | None = None


@dataclass
class Registry:
    groups: dict[str, RegistryGroup] = field(default_factory=dict)
    accounts: dict[str, RegistryAccount] = field(default_factory=dict)
    account_aliases: dict[str, str] = field(default_factory=dict)
    group_aliases: dict[str, list[str]] = field(default_factory=dict)
    account_by_name: dict[str, str] = field(default_factory=dict)
    group_by_path: dict[str, str] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def empty(cls) -> "Registry":
        return cls()

    def active_accounts(self) -> list[RegistryAccount]:
        return [account for account in self.accounts.values() if account.status == "active"]

    def resolve(self, ref: str) -> tuple[str, str] | None:
        """Resolve an id, alias, account name or group path to ``(kind, id)``."""
        ref = ref.strip()
        if not ref:
            return None
        if ref in self.accounts:
            return ("account", ref)
        if ref in self.groups:
            return ("group", ref)
        key = alias_key(ref)
        if key in self.account_aliases:
            return ("account", self.account_aliases[key])
        if ref in self.account_by_name:
            return ("account", self.account_by_name[ref])
        if ref in self.group_by_path:
            return ("group", self.group_by_path[ref])
        candidates = self.group_aliases.get(key) or []
        if len(candidates) == 1:
            return ("group", candidates[0])
        if len(candidates) > 1:
            raise KernelError(
                REGISTRY_INVALID,
                f"group alias is ambiguous: {ref!r}",
                {"candidates": sorted(candidates)},
            )
        return None


def alias_key(value: str) -> str:
    """Normalization used only for uniqueness/resolution, never for storage."""
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def _fail(message: str, **details: Any) -> None:
    raise KernelError(REGISTRY_INVALID, message, details)


def _text(value: Any, field_name: str, *, max_len: int = 64) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{field_name} must be a non-empty string")
    if value != value.strip():
        _fail(f"{field_name} must not have leading or trailing whitespace")
    if len(value) > max_len:
        _fail(f"{field_name} is longer than {max_len} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        _fail(f"{field_name} contains control characters")
    return value


def _identifier(value: Any, field_name: str, prefix: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value) or not value.startswith(prefix):
        _fail(f"{field_name} must match {prefix}<id> and contain only letters/digits/underscore/hyphen")
    return value


def _segment(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not SEGMENT_RE.match(value):
        _fail(f"{field_name} must match [A-Z][A-Za-z0-9-]*")
    return value


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


def _date_or_none(value: Any, field_name: str) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            _fail(f"{field_name} must be an ISO date (YYYY-MM-DD)")
    _fail(f"{field_name} must be an ISO date (YYYY-MM-DD)")
    raise AssertionError("unreachable")


def _string_list(value: Any, field_name: str, *, max_items: int = 50) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        _fail(f"{field_name} must be a list of non-empty strings")
    if len(value) > max_items:
        _fail(f"{field_name} has more than {max_items} items")
    if len(set(value)) != len(value):
        _fail(f"{field_name} contains duplicates")
    return tuple(value)


def _parse_policy(account_name: str, raw: Any) -> AccountPolicy:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _fail(f"{account_name}.policy must be an object")
    # Reuse the schema-v1 policy validator (currencies, tags, cadence, bounds).
    try:
        contract = parse_contract(
            {"schema_version": 1, "accounts": {account_name: {"policy": raw}}}
        )
    except KernelError as exc:
        raise KernelError(
            REGISTRY_INVALID,
            f"{account_name}.policy: {exc.message}",
            exc.details,
        ) from exc
    return contract.accounts[account_name]


def _parse_date_fields(raw: Mapping[str, Any], account_name: str) -> tuple[dt.date | None, dt.date | None]:
    opened = _date_or_none(raw.get("open_date"), f"{account_name}.open_date")
    closed = _date_or_none(raw.get("close_date"), f"{account_name}.close_date")
    if opened is not None and closed is not None and closed <= opened:
        _fail(f"{account_name}.close_date must be after open_date")
    return opened, closed


def _parse_currencies(raw: Mapping[str, Any], account_name: str) -> tuple[str, ...]:
    currencies = _string_list(raw.get("currencies"), f"{account_name}.currencies")
    for currency in currencies:
        if not _CURRENCY_RE.match(currency):
            _fail(f"{account_name}.currencies has invalid token: {currency!r}")
    return currencies


def _parse_booking(raw: Mapping[str, Any], account_name: str) -> str | None:
    booking = raw.get("booking")
    if booking is None:
        return None
    if not isinstance(booking, str) or booking not in _BOOKINGS:
        _fail(f"{account_name}.booking must be one of {', '.join(sorted(_BOOKINGS))}")
    return booking


def _resolve_groups(
    seeds: dict[str, dict[str, Any]]
) -> tuple[dict[str, RegistryGroup], dict[str, str], dict[str, str]]:
    resolved: dict[str, RegistryGroup] = {}
    paths: dict[str, str] = {}
    types: dict[str, str] = {}

    def visit(group_id: str, stack: tuple[str, ...]) -> RegistryGroup:
        if group_id in resolved:
            return resolved[group_id]
        if group_id in stack:
            _fail(f"group parent cycle: {' -> '.join((*stack, group_id))}")
        seed = seeds[group_id]
        parent = seed["parent"]
        if parent is None:
            group_type = seed.get("type")
            if group_type not in TYPES:
                _fail(f"root group {group_id} must declare type in {', '.join(TYPES)}")
            if seed["segment"] != group_type:
                _fail(f"root group {group_id} segment must equal its type ({group_type})")
            path = group_type
            depth = 1
        else:
            if parent not in seeds:
                _fail(f"group {group_id} references unknown parent: {parent}")
            parent_group = visit(parent, (*stack, group_id))
            parent_type = parent_group.type
            declared = seed.get("type")
            if declared is not None and declared != parent_type:
                _fail(f"group {group_id} type {declared!r} does not match root type {parent_type!r}")
            path = f"{parent_group.path}:{seed['segment']}"
            depth = parent_group.path.count(":") + 2
            group_type = parent_type
        if depth > MAX_GROUP_DEPTH + 1:
            _fail(f"group {group_id} is deeper than {MAX_GROUP_DEPTH} levels under its type")
        group = RegistryGroup(
            id=group_id,
            alias=seed["alias"],
            segment=seed["segment"],
            parent=parent,
            type=group_type,
            path=path,
        )
        resolved[group_id] = group
        paths[path] = group_id
        types[group_id] = group_type
        return group

    for group_id in seeds:
        visit(group_id, ())
    return resolved, paths, types


def parse_registry(raw: Any) -> Registry:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _fail("accounts.yaml must contain an object")
    allowed = {"schema_version", "groups", "accounts"}
    unknown = set(raw) - allowed
    if unknown:
        _fail(f"accounts.yaml has unsupported fields: {', '.join(sorted(unknown))}")
    version = raw.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        _fail(f"unsupported accounts schema_version: {version!r}")

    group_items = raw.get("groups") or []
    account_items = raw.get("accounts") or []
    if not isinstance(group_items, list):
        _fail("groups must be a list")
    if not isinstance(account_items, list):
        _fail("accounts must be a list")

    seeds: dict[str, dict[str, Any]] = {}
    group_alias_seed: dict[tuple[str | None, str], str] = {}
    for index, item in enumerate(group_items):
        if not isinstance(item, dict):
            _fail(f"groups[{index}] must be an object")
        unknown = set(item) - {"id", "alias", "segment", "parent", "type"}
        if unknown:
            _fail(f"groups[{index}] has unsupported fields: {', '.join(sorted(unknown))}")
        group_id = _identifier(item.get("id"), f"groups[{index}].id", "grp_")
        if group_id in seeds:
            _fail(f"duplicate group id: {group_id}")
        alias = _text(item.get("alias"), f"groups[{index}].alias")
        segment = _segment(item.get("segment"), f"groups[{index}].segment")
        parent = item.get("parent")
        if parent is not None and not isinstance(parent, str):
            _fail(f"groups[{index}].parent must be a group id or null")
        key = (parent, alias_key(alias))
        if key in group_alias_seed:
            _fail(f"duplicate group alias under the same parent: {alias!r}")
        group_alias_seed[key] = group_id
        seeds[group_id] = {
            "id": group_id,
            "alias": alias,
            "segment": segment,
            "parent": parent,
            "type": item.get("type"),
        }

    groups, group_paths, group_types = _resolve_groups(seeds)

    siblings: dict[tuple[str | None, str], str] = {}
    for group in groups.values():
        key = (group.parent, group.segment)
        if key in siblings:
            _fail(f"duplicate segment under one parent: {group.segment}")
        siblings[key] = group.id

    accounts: dict[str, RegistryAccount] = {}
    account_aliases: dict[str, str] = {}
    account_by_name: dict[str, str] = {}
    ids = set(groups)
    for index, item in enumerate(account_items):
        if not isinstance(item, dict):
            _fail(f"accounts[{index}] must be an object")
        allowed_fields = {
            "id",
            "alias",
            "segment",
            "mode",
            "parent",
            "status",
            "successor_of",
            "policy",
            "open_date",
            "close_date",
            "currencies",
            "booking",
        }
        unknown = set(item) - allowed_fields
        if unknown:
            _fail(f"accounts[{index}] has unsupported fields: {', '.join(sorted(unknown))}")
        account_id = _identifier(item.get("id"), f"accounts[{index}].id", "acc_")
        if account_id in ids:
            _fail(f"duplicate id: {account_id}")
        ids.add(account_id)
        alias = _text(item.get("alias"), f"accounts[{index}].alias")
        segment = _segment(item.get("segment"), f"accounts[{index}].segment")
        mode = item.get("mode")
        if mode not in _MODES:
            _fail(f"accounts[{index}].mode must be one of {', '.join(sorted(_MODES))}")
        if mode == "opaque" and not OPAQUE_RE.match(segment):
            _fail(f"opaque segment must match Acct-<8..12 lowercase hex>: {segment!r}")
        if mode == "semantic" and OPAQUE_RE.match(segment):
            _fail(f"semantic segment must not use the opaque Acct- prefix: {segment!r}")
        parent = item.get("parent")
        if not isinstance(parent, str) or parent not in groups:
            _fail(f"accounts[{index}].parent must reference an existing group")
        status = item.get("status")
        if status not in _STATUSES:
            _fail(f"accounts[{index}].status must be one of {', '.join(sorted(_STATUSES))}")
        key = (parent, segment)
        if key in siblings:
            _fail(f"duplicate segment under one parent: {segment}")
        siblings[key] = account_id
        account_name = f"{groups[parent].path}:{segment}"
        if not bc_account.is_valid(account_name):
            _fail(f"derived account name is not valid Beancount: {account_name!r}")
        if account_name in account_by_name:
            _fail(f"duplicate account name: {account_name}")
        alias_key_value = alias_key(alias)
        if alias_key_value in account_aliases:
            _fail(f"duplicate account alias: {alias!r}")
        account_aliases[alias_key_value] = account_id
        successor_of = item.get("successor_of")
        if successor_of is not None and not isinstance(successor_of, str):
            _fail(f"accounts[{index}].successor_of must be an account id or null")
        opened, closed = _parse_date_fields(item, account_name)
        if status == "active" and closed is not None:
            _fail(f"active account {account_name} must not declare close_date")
        accounts[account_id] = RegistryAccount(
            id=account_id,
            alias=alias,
            segment=segment,
            mode=mode,
            parent=parent,
            status=status,
            successor_of=successor_of,
            policy=_parse_policy(account_name, item.get("policy")),
            account=account_name,
            type=group_types[parent],
            open_date=opened,
            close_date=closed,
            currencies=_parse_currencies(item, account_name),
            booking=_parse_booking(item, account_name),
        )
        account_by_name[account_name] = account_id

    _validate_successors(accounts)

    group_aliases: dict[str, list[str]] = {}
    for group in groups.values():
        group_aliases.setdefault(alias_key(group.alias), []).append(group.id)

    return Registry(
        groups=groups,
        accounts=accounts,
        account_aliases=account_aliases,
        group_aliases=group_aliases,
        account_by_name=account_by_name,
        group_by_path=group_paths,
        schema_version=SCHEMA_VERSION,
    )


def _validate_successors(accounts: Mapping[str, RegistryAccount]) -> None:
    for account in accounts.values():
        if account.successor_of is None:
            continue
        predecessor = accounts.get(account.successor_of)
        if predecessor is None:
            _fail(f"{account.account}.successor_of references unknown account: {account.successor_of}")
        if predecessor.id == account.id:
            _fail(f"{account.account}.successor_of must not reference itself")
        if predecessor.status != "archived":
            _fail(f"successor predecessor must be archived: {predecessor.account}")
        if account.status != "active":
            _fail(f"successor account must be active: {account.account}")
        if predecessor.type != account.type:
            _fail(f"successor must keep the same root type: {predecessor.account} -> {account.account}")
        seen = {account.id}
        current = predecessor
        while current.successor_of is not None:
            if current.id in seen:
                _fail(f"successor cycle detected at {current.account}")
            seen.add(current.id)
            nxt = accounts.get(current.successor_of)
            if nxt is None:
                break
            current = nxt


def parse_registry_text(text: str) -> Registry:
    if not isinstance(text, str) or not text.strip():
        _fail("accounts.yaml must contain a YAML object")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise KernelError(REGISTRY_INVALID, f"cannot parse accounts.yaml: {exc}") from exc
    return parse_registry(raw)


def load_registry(path: Path) -> Registry:
    if not path.exists():
        return Registry.empty()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KernelError(REGISTRY_INVALID, f"cannot read accounts.yaml: {exc}") from exc
    return parse_registry_text(text)


def registry_revision(path: Path) -> str:
    if not path.exists():
        return "sha256:absent"
    try:
        return "sha256:" + sha256_file(path)
    except OSError as exc:  # pragma: no cover - environmental
        raise KernelError(REGISTRY_INVALID, f"cannot hash accounts.yaml: {exc}") from exc


def default_registry_yaml() -> str:
    return (
        "# LedgerKernel account registry (schema_version 2, draft).\n"
        "# The registry is the model source of truth; the ledger stays append-only.\n"
        "schema_version: 2\n"
        "groups: []\n"
        "accounts: []\n"
    )


def registry_view(registry: Registry, load: LoadResult, *, revision: str) -> dict[str, Any]:
    managed = set(registry.account_by_name)
    unmanaged = sorted(set(load.accounts) - managed)
    warnings: list[dict[str, Any]] = []
    for account in sorted(registry.active_accounts(), key=lambda item: item.account):
        if account.account not in load.accounts:
            warnings.append(
                {
                    "code": "REGISTRY_ACCOUNT_NOT_OPENED",
                    "severity": "warning",
                    "account": account.account,
                    "message": f"registry account is not opened in the ledger: {account.account}",
                }
            )
    for name in unmanaged:
        warnings.append(
            {
                "code": "REGISTRY_UNMANAGED_ACCOUNT",
                "severity": "info",
                "account": name,
                "message": f"ledger account is not declared in the registry: {name}",
            }
        )
    return {
        "schema_version": registry.schema_version,
        "registry_revision": revision,
        "valid": True,
        "error": None,
        "counts": {
            "groups": len(registry.groups),
            "accounts": len(registry.accounts),
            "active": len(registry.active_accounts()),
            "archived": len(registry.accounts) - len(registry.active_accounts()),
            "unmanaged": len(unmanaged),
        },
        "groups": [
            {
                "id": group.id,
                "alias": group.alias,
                "segment": group.segment,
                "parent": group.parent,
                "type": group.type,
                "path": group.path,
            }
            for group in sorted(registry.groups.values(), key=lambda item: item.path)
        ],
        "accounts": [
            {
                "id": account.id,
                "alias": account.alias,
                "account": account.account,
                "segment": account.segment,
                "mode": account.mode,
                "parent": account.parent,
                "type": account.type,
                "status": account.status,
                "successor_of": account.successor_of,
                "open_date": None if account.open_date is None else account.open_date.isoformat(),
                "close_date": None if account.close_date is None else account.close_date.isoformat(),
                "currencies": list(account.currencies),
                "booking": account.booking,
                "policy": account_contract_view(account.policy),
            }
            for account in sorted(registry.accounts.values(), key=lambda item: item.account)
        ],
        "unmanaged_accounts": unmanaged,
        "warnings": warnings,
    }


def plan_registry(registry: Registry, load: LoadResult, *, revision: str) -> dict[str, Any]:
    """Read-only diff between the registry (desired) and the ledger (actual)."""
    items: list[dict[str, Any]] = []
    managed: set[str] = set()

    def add(
        code: str,
        severity: str,
        account: str,
        alias: str | None,
        message: str,
        **details: Any,
    ) -> None:
        items.append(
            {
                "code": code,
                "severity": severity,
                "account": account,
                "alias": alias,
                "message": message,
                "details": details,
            }
        )

    for account in sorted(registry.accounts.values(), key=lambda item: item.account):
        managed.add(account.account)
        info = load.accounts.get(account.account)
        if account.status == "active":
            if info is None:
                add(
                    "MISSING_OPEN",
                    "action",
                    account.account,
                    account.alias,
                    "registry account is active but has no open directive",
                )
                continue
            if info.close_date is not None:
                add(
                    "LEDGER_CLOSED",
                    "warning",
                    account.account,
                    account.alias,
                    "registry account is active but the ledger account is closed",
                )
            elif account.open_date is not None and info.open_date != account.open_date:
                add(
                    "OPEN_DATE_DRIFT",
                    "warning",
                    account.account,
                    account.alias,
                    "open date differs from the registry",
                    registry=account.open_date.isoformat(),
                    ledger=None if info.open_date is None else info.open_date.isoformat(),
                )
            if account.currencies and tuple(info.currencies) != account.currencies:
                add(
                    "CURRENCY_DRIFT",
                    "warning",
                    account.account,
                    account.alias,
                    "open directive currencies differ from the registry",
                    registry=list(account.currencies),
                    ledger=list(info.currencies),
                )
            meta_alias = info.meta.get("alias")
            if isinstance(meta_alias, str) and meta_alias != account.alias:
                add(
                    "ALIAS_DRIFT",
                    "warning",
                    account.account,
                    account.alias,
                    "open directive alias metadata differs from the registry",
                    ledger=meta_alias,
                )
        else:
            if info is None:
                continue
            if info.close_date is None:
                add(
                    "MISSING_CLOSE",
                    "action",
                    account.account,
                    account.alias,
                    "registry account is archived but the ledger account is still open",
                )
            elif account.close_date is not None and info.close_date != account.close_date:
                add(
                    "CLOSE_DATE_DRIFT",
                    "warning",
                    account.account,
                    account.alias,
                    "close date differs from the registry",
                    registry=account.close_date.isoformat(),
                    ledger=info.close_date.isoformat(),
                )

    for name in sorted(set(load.accounts) - managed):
        add(
            "UNMANAGED_ACCOUNT",
            "info",
            name,
            None,
            "ledger account is not declared in the registry",
        )

    counts = Counter(item["code"] for item in items)
    actions = {
        "open": [item["account"] for item in items if item["code"] == "MISSING_OPEN"],
        "close": [item["account"] for item in items if item["code"] == "MISSING_CLOSE"],
        "relabel": [item["account"] for item in items if item["code"] == "ALIAS_DRIFT"],
    }
    return {
        "schema_version": registry.schema_version,
        "registry_revision": revision,
        "ledger_revision": load.revision,
        "ok": not items,
        "counts": dict(sorted(counts.items())),
        "items": items,
        "actions": actions,
    }
