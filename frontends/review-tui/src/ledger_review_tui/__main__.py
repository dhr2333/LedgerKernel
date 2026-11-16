"""Entry point: read-only review TUI, or a non-interactive --list summary."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from . import __version__, model
from .kernel_cli import KernelCli, KernelCliError

ClientFactory = Callable[[str, str], KernelCli]

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger-review-tui", description="Read-only LedgerKernel review frontend; approval always requires typing the draft id.")
    parser.add_argument("--ledger", required=True, help="ledger directory passed to ledger-kernel --ledger")
    parser.add_argument("--kernel-cmd", default="ledger-kernel", help="kernel CLI executable (default: ledger-kernel)")
    parser.add_argument("--list", action="store_true", help="print grouped pending drafts and exit (non-interactive, e.g. over SSH)")
    parser.add_argument("--version", action="version", version=f"ledger-review-tui {__version__}")
    return parser

def list_command(client: KernelCli) -> int:
    """Print the grouped queue; never approves or writes anything."""
    try:
        payload = client.review_by_account()
    except KernelCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(model.render_list(payload))
    return 0

def main(argv: Sequence[str] | None = None, *, client_factory: ClientFactory | None = None) -> int:
    args = _parser().parse_args(argv)
    client = (client_factory or (lambda ledger, cmd: KernelCli(ledger=ledger, kernel_cmd=cmd)))(args.ledger, args.kernel_cmd)
    if args.list:
        return list_command(client)
    from .tui import run as run_tui  # lazy: --list works where curses is unavailable

    return run_tui(client)
if __name__ == "__main__":
    raise SystemExit(main())
