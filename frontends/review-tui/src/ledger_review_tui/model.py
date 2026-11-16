"""Pure data and text helpers for the review TUI (CLI JSON only; no curses or kernel imports)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class ReviewRow:
    """One queue line: an account header or a draft row."""

    kind: str
    label: str
    draft_id: str | None = None
    state: str | None = None
    validation_status: str | None = None

def _amount(row: Mapping[str, Any]) -> str:
    amount = row.get("amount")
    return "-" if amount is None else f"{amount} {row.get('currency') or ''}".strip()

def group_label(group: Mapping[str, Any]) -> str:
    balances = group.get("balances") or {}
    balance_text = ", ".join(f"{value} {code}" for code, value in sorted(balances.items())) or "-"
    status = (group.get("reconciliation") or {}).get("status") or "?"
    return f"{group.get('account')}  {group.get('lifecycle') or '?'}  {balance_text}  reconciliation {status}"

def build_rows(payload: Mapping[str, Any]) -> list[ReviewRow]:
    """Flatten review --by-account output into account and draft rows."""
    rows: list[ReviewRow] = []
    for group in payload.get("groups") or []:
        rows.append(ReviewRow("group", group_label(group)))
        for draft in group.get("pending_drafts") or []:
            label = f"{draft.get('draft_id')}  {draft.get('date') or '-'}  {_amount(draft)}  {draft.get('narration') or '-'}"
            rows.append(ReviewRow("draft", label, draft.get("draft_id"), draft.get("state"), draft.get("validation_status")))
    return rows

def selected_draft_id(rows: Sequence[ReviewRow], index: int) -> str | None:
    """Draft id under the cursor, or None on a group header or empty list."""
    return rows[index].draft_id if 0 <= index < len(rows) and rows[index].kind == "draft" else None

def move_selection(rows: Sequence[ReviewRow], index: int, delta: int) -> int:
    return max(0, min(len(rows) - 1, index + delta)) if rows else 0

def confirm_string(draft_ids: Sequence[str]) -> str:
    """Exact --confirm value expected by the kernel CLI: ids joined by ','."""
    return ",".join(str(draft_id) for draft_id in draft_ids)

def checks_lines(checks: Iterable[Mapping[str, Any]]) -> list[str]:
    lines = [f"[{c.get('severity')}] {c.get('code')}: {c.get('message')}" for c in checks]
    return lines or ["no blocking or warning checks"]

def diff_lines(diff: Mapping[str, Any] | None) -> list[str]:
    if not diff:
        return ["(no diff recorded)"]
    lines: list[str] = []
    if diff.get("changed_files"):
        lines.append("files: " + ", ".join(str(path) for path in diff["changed_files"]))
    for delta in diff.get("deltas") or []:
        lines.append(f"balance: {delta.get('account')}  {delta.get('before')} -> {delta.get('after')}  ({delta.get('delta')} {delta.get('currency')})")
    if diff.get("file_diff"):
        lines.append("file diff:")
        lines.extend("  " + line for line in str(diff["file_diff"]).splitlines()[:40])
    return lines or ["(no diff recorded)"]

def detail_lines(detail: Mapping[str, Any]) -> list[str]:
    """Render one review <draft_id> detail item for the pager."""
    lines = [f"draft {detail.get('draft_id')}  state={detail.get('state')}  validation={detail.get('validation_status') or 'none'}", "", "fragment:"]
    fragment = str(detail.get("fragment") or "").strip("\n")
    lines.extend("  " + line for line in (fragment.splitlines() or ["(not validated yet)"]))
    lines.extend(["", "checks:"])
    lines.extend("  " + line for line in checks_lines(detail.get("checks") or []))
    lines.extend(["", "diff:"])
    lines.extend("  " + line for line in diff_lines(detail.get("diff") or {}))
    return lines

def render_list(payload: Mapping[str, Any]) -> str:
    """Readable non-interactive summary of a review --by-account payload."""
    groups = payload.get("groups") or []
    if not groups:
        return str(payload.get("note") or "no pending drafts")
    lines: list[str] = []
    for group in groups:
        lines.append(f"== {group_label(group)}")
        drafts = group.get("pending_drafts") or []
        if not drafts:
            lines.append("   pending: none")
        for draft in drafts:
            lines.append(f"   {draft.get('draft_id')}  {draft.get('date') or '-'}  {_amount(draft)}  {draft.get('narration') or '-'}  [{draft.get('state')} / {draft.get('validation_status') or 'unvalidated'}]")
        for warning in group.get("warnings") or []:
            lines.append(f"   warning: {warning.get('code')}  {warning.get('message')}  [{warning.get('draft_id')}]")
    return "\n".join(lines)
