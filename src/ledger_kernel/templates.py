"""Reusable draft templates (``.ledger-kernel/templates.yaml``).

Templates prefill the fields that repeat across entries of the same kind
(merchant, accounts, tags, payment method). They never bypass validation:
``ledger-kernel drafts submit --template`` expands a template into an ordinary
draft payload, and the kernel validates it like any other draft.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import yaml

from .errors import KernelError, TEMPLATE_INVALID

SCHEMA_VERSION = 1
FILENAME = "templates.yaml"
_ALLOWED_TOP = {"schema_version", "templates"}
_ALLOWED_TEMPLATE = {"payee", "narration", "tags", "currency", "postings"}
_ALLOWED_POSTING = {"account", "units"}
_PLACEHOLDERS = {"amount", "-amount"}


@dataclass(frozen=True)
class Template:
    name: str
    payee: str | None
    narration: str | None
    tags: tuple[str, ...]
    currency: str
    postings: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "payee": self.payee,
            "narration": self.narration,
            "tags": list(self.tags),
            "currency": self.currency,
            "postings": [{"account": account, "units": units} for account, units in self.postings],
        }


def templates_path(meta_dir: Path) -> Path:
    return meta_dir / FILENAME


def load_templates(path: Path) -> dict[str, Template]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise KernelError(TEMPLATE_INVALID, f"cannot read templates: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise KernelError(TEMPLATE_INVALID, "templates file must be a mapping")
    unknown = set(data) - _ALLOWED_TOP
    if unknown:
        raise KernelError(TEMPLATE_INVALID, f"templates has unsupported fields: {', '.join(sorted(unknown))}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise KernelError(TEMPLATE_INVALID, f"unsupported templates schema_version: {data.get('schema_version')!r}")
    raw_templates = data.get("templates") or {}
    if not isinstance(raw_templates, dict):
        raise KernelError(TEMPLATE_INVALID, "templates must be a mapping")
    return {str(name): _parse_template(str(name), raw) for name, raw in raw_templates.items()}


def _parse_template(name: str, raw: Any) -> Template:
    if not isinstance(raw, dict):
        raise KernelError(TEMPLATE_INVALID, f"template {name} must be a mapping")
    unknown = set(raw) - _ALLOWED_TEMPLATE
    if unknown:
        raise KernelError(
            TEMPLATE_INVALID, f"template {name} has unsupported fields: {', '.join(sorted(unknown))}"
        )
    payee = raw.get("payee")
    narration = raw.get("narration")
    tags = raw.get("tags") or []
    currency = str(raw.get("currency") or "CNY")
    for field_name, value in (("payee", payee), ("narration", narration)):
        if value is not None and not isinstance(value, str):
            raise KernelError(TEMPLATE_INVALID, f"template {name}.{field_name} must be a string")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise KernelError(TEMPLATE_INVALID, f"template {name}.tags must be a list of strings")
    postings = raw.get("postings")
    if not isinstance(postings, list) or len(postings) < 2:
        raise KernelError(TEMPLATE_INVALID, f"template {name}.postings must contain at least two postings")
    parsed: list[tuple[str, str]] = []
    for index, posting in enumerate(postings):
        if not isinstance(posting, dict):
            raise KernelError(TEMPLATE_INVALID, f"template {name}.postings[{index}] must be a mapping")
        unknown_posting = set(posting) - _ALLOWED_POSTING
        if unknown_posting:
            raise KernelError(
                TEMPLATE_INVALID,
                f"template {name}.postings[{index}] has unsupported fields: {', '.join(sorted(unknown_posting))}",
            )
        account = posting.get("account")
        units = posting.get("units")
        if not isinstance(account, str) or not account.strip():
            raise KernelError(TEMPLATE_INVALID, f"template {name}.postings[{index}].account is required")
        if not isinstance(units, str) or not units.strip():
            raise KernelError(TEMPLATE_INVALID, f"template {name}.postings[{index}].units is required")
        if units not in _PLACEHOLDERS:
            try:
                Decimal(units)
            except InvalidOperation as exc:
                raise KernelError(
                    TEMPLATE_INVALID,
                    f"template {name}.postings[{index}].units must be 'amount', '-amount' or a decimal string",
                ) from exc
        parsed.append((account.strip(), units.strip()))
    if not any(units in _PLACEHOLDERS for _, units in parsed):
        # A template without a placeholder is still valid (fixed-amount entry),
        # but --amount is then rejected instead of silently ignored.
        pass
    return Template(
        name=name,
        payee=payee,
        narration=narration,
        tags=tuple(tags),
        currency=currency,
        postings=tuple(parsed),
    )


def build_payload(
    template: Template,
    *,
    amount: str | None,
    entry_date: date,
    payee: str | None = None,
    narration: str | None = None,
    tags: Iterable[str] = (),
) -> dict[str, Any]:
    needs_amount = any(units in _PLACEHOLDERS for _, units in template.postings)
    if needs_amount and amount is None:
        raise KernelError(TEMPLATE_INVALID, f"template {template.name} requires --amount")
    if not needs_amount and amount is not None:
        raise KernelError(TEMPLATE_INVALID, f"template {template.name} uses fixed amounts; drop --amount")
    rendered_amount: str | None = None
    if amount is not None:
        try:
            value = Decimal(amount)
        except InvalidOperation as exc:
            raise KernelError(TEMPLATE_INVALID, f"invalid --amount: {amount}") from exc
        if not value.is_finite() or value <= 0:
            raise KernelError(TEMPLATE_INVALID, "--amount must be a positive decimal")
        rendered_amount = format(value, "f")

    postings: list[dict[str, str]] = []
    for account, units in template.postings:
        if units == "amount":
            resolved = rendered_amount
        elif units == "-amount":
            resolved = "-" + str(rendered_amount)
        else:
            resolved = units
        postings.append({"account": account, "units": str(resolved), "currency": template.currency})

    transaction: dict[str, Any] = {
        "date": entry_date.isoformat(),
        "narration": narration if narration is not None else (template.narration or template.name),
        "postings": postings,
    }
    resolved_payee = payee if payee is not None else template.payee
    if resolved_payee:
        transaction["payee"] = resolved_payee
    merged_tags = list(dict.fromkeys([*template.tags, *tags]))
    if merged_tags:
        transaction["tags"] = merged_tags

    return {
        "schema_version": 1,
        "operation": "append_transactions",
        "transactions": [transaction],
        "confidence": None,
        "rationale_summary": f"draft template: {template.name}",
    }
