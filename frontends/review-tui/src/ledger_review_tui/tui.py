"""Minimal standard-library curses UI: read-only queue plus typed approval."""

from __future__ import annotations

import curses
import sys
from typing import Any

from . import model
from .kernel_cli import KernelCli, KernelCliError

def _add(stdscr: Any, y: int, text: str, attr: int = 0) -> None:
    try:
        height, width = stdscr.getmaxyx()
        if 0 <= y < height and width > 1:
            stdscr.addnstr(y, 0, text, width - 1, attr)
    except curses.error:
        pass

def _pager(stdscr: Any, title: str, lines: list[str]) -> None:
    offset = 0
    while True:
        height, _ = stdscr.getmaxyx()
        visible = max(1, height - 2)
        stdscr.erase()
        _add(stdscr, 0, title, curses.A_BOLD)
        for row, line in enumerate(lines[offset:offset + visible]):
            _add(stdscr, 1 + row, line)
        _add(stdscr, height - 1, "up/down scroll  q/Enter back")
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("q"), 10, 13, 27, curses.KEY_ENTER):
            return
        if key in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            offset = min(max(0, len(lines) - 1), offset + 1)

class ReviewTui:
    def __init__(self, client: KernelCli) -> None:
        self.client = client
        self.rows: list[model.ReviewRow] = []
        self.index = 0
        self.status = "loading"
        self.error = ""
    def refresh(self) -> None:
        try:
            self.rows = model.build_rows(self.client.review_by_account())
            self.index = min(self.index, max(0, len(self.rows) - 1))
            self.error = ""
            self.status = f"loaded {len(self.rows)} row(s)"
        except KernelCliError as exc:
            self.rows, self.index, self.error = [], 0, str(exc)
    def _detail(self, stdscr: Any) -> None:
        draft_id = model.selected_draft_id(self.rows, self.index)
        if draft_id is None:
            self.status = "select a draft row first"
            return
        try:
            detail = self.client.draft_detail(draft_id)
        except KernelCliError as exc:
            self.error = str(exc)
            return
        self.error = ""
        _pager(stdscr, f"draft {draft_id} (read-only)", model.detail_lines(detail))
    def _validate(self, stdscr: Any) -> None:
        draft_id = model.selected_draft_id(self.rows, self.index)
        if draft_id is None:
            self.status = "select a draft row first"
            return
        try:
            validation = self.client.validate_draft(draft_id)
        except KernelCliError as exc:
            self.error = str(exc)
            return
        self.refresh()
        self.status = f"validated {draft_id}: {validation.get('status')}"
        _pager(stdscr, f"validation {draft_id}", model.checks_lines(validation.get("checks") or []))
    def _approve(self, stdscr: Any) -> None:
        draft_id = model.selected_draft_id(self.rows, self.index)
        if draft_id is None:
            self.status = "select a draft row first"
            return
        expected = model.confirm_string([draft_id])
        height, width = stdscr.getmaxyx()
        label = f"type exactly {expected!r} to approve: "
        try:
            curses.echo()
            _add(stdscr, height - 1, label)
            raw = stdscr.getstr(height - 1, min(len(label), max(0, width - 2)), max(1, width - len(label) - 2))
        except curses.error:
            raw = b""
        finally:
            curses.noecho()
        if raw.decode("utf-8", "replace").strip() != expected:
            self.status = "confirmation did not match; approval cancelled"
            return
        try:
            self.client.approve_drafts([draft_id], expected)
            self.refresh()
            self.status = f"approved {draft_id}"
        except KernelCliError as exc:
            self.error = str(exc)
    def draw(self, stdscr: Any) -> None:
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        if height < 5 or width < 30:
            _add(stdscr, 0, "terminal too small; enlarge it, q quits")
            stdscr.refresh()
            return
        _add(stdscr, 0, "LedgerKernel review (read-only)", curses.A_BOLD)
        visible = max(1, height - 3)
        start = max(0, self.index - visible + 1)
        for offset, row in enumerate(self.rows[start:start + visible]):
            selected = start + offset == self.index
            prefix = "> " if selected else "  "
            attr = curses.A_REVERSE if selected else 0
            if row.kind == "group":
                _add(stdscr, 1 + offset, f"{prefix}== {row.label}", curses.A_BOLD | attr)
            else:
                _add(stdscr, 1 + offset, f"{prefix}   {row.label}  [{row.state} / {row.validation_status or 'unvalidated'}]", attr)
        if not self.rows:
            _add(stdscr, 1, "no rows; press r to refresh")
        _add(stdscr, height - 2, self.error or self.status, curses.A_BOLD)
        _add(stdscr, height - 1, "up/down move  Enter detail  v validate  a approve  r refresh  q quit")
        stdscr.refresh()
    def run(self, stdscr: Any) -> int:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.refresh()
        while True:
            self.draw(stdscr)
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return 0
            if key in (curses.KEY_UP, ord("k")):
                self.index = model.move_selection(self.rows, self.index, -1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.index = model.move_selection(self.rows, self.index, 1)
            elif key in (10, 13, curses.KEY_ENTER):
                self._detail(stdscr)
            elif key == ord("v"):
                self._validate(stdscr)
            elif key == ord("a"):
                self._approve(stdscr)
            elif key == ord("r"):
                self.refresh()

def run(client: KernelCli) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("review TUI needs an interactive terminal; use --list instead", file=sys.stderr)
        return 2
    try:
        curses.wrapper(ReviewTui(client).run)
    except KeyboardInterrupt:
        return 130
    except curses.error as exc:
        print(f"review TUI failed: {exc}; use --list instead", file=sys.stderr)
        return 2
    return 0
