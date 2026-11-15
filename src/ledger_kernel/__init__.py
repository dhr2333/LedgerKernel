"""LedgerKernel: a local, auditable control layer around a Beancount ledger.

The ledger file is the single source of financial truth. The kernel is the only
controlled entry point for *automated* writes; users keep the highest right to
edit files directly, and such edits are observed, not overwritten.
"""

from __future__ import annotations

__version__ = "0.1.0"

KERNEL_NAME = "ledger-kernel"

__all__ = ["__version__", "KERNEL_NAME"]
