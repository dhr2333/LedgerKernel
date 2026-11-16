"""Thin, version-pinned adapter over Beancount's official load/validate chain.

The kernel does not re-implement Beancount grammar, booking, balance checks or
directive semantics. Everything semantic comes from ``beancount.loader``.
"""

from __future__ import annotations

import datetime as dt
import enum
import os
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from beancount import loader
from beancount.core import data
from beancount.core.amount import Amount
from beancount.core.position import Cost

from .errors import CLOSURE_INCONSISTENT, LEDGER_LOAD_FAILED, KernelError
from .revision import engine_fingerprint, manifest_for, revision_of
from .util import is_within, relative_posix, sha256_file

# Option keys that are paths, hashes or object instances. They are excluded from
# the semantic options signature because the dry-run happens in a temp directory.
_VOLATILE_OPTION_KEYS = {
    "filename",
    "include",
    "input_hash",
    "dcontext",
    "display_precision",
}


@dataclass(frozen=True)
class AccountInfo:
    name: str
    open_date: dt.date | None
    close_date: dt.date | None
    currencies: tuple[str, ...]
    booking: str | None
    source_file: str | None
    source_line: int | None

    def is_open_on(self, date: dt.date) -> bool:
        if self.open_date is None or date < self.open_date:
            return False
        if self.close_date is not None and date >= self.close_date:
            return False
        return True


@dataclass
class LoadResult:
    root_file: Path
    ledger_dir: Path
    entries: list[Any]
    errors: list[Any]
    options: dict[str, Any]
    include_files: list[Path]
    manifest: dict[str, str]
    engine_fingerprint: str
    revision: str
    accounts: dict[str, AccountInfo] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def closure_source(self) -> str:
        return str(self.options.get("_kernel_closure_source", "options.include"))

    def error_records(self, errors: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        return BeancountAdapter.error_records(self.errors if errors is None else errors)

    def entry_counter(self, entries: Iterable[Any] | None = None) -> Counter:
        return BeancountAdapter.entry_counter(
            self.entries if entries is None else entries, self.ledger_dir
        )

    def options_signature(self, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return BeancountAdapter.options_signature(self.options if options is None else options)


class BeancountAdapter:
    def __init__(self, ledger_dir: Path):
        self.ledger_dir = ledger_dir.resolve()

    # ------------------------------------------------------------------ load
    def load(self, root_file: Path | None = None) -> LoadResult:
        root = (root_file or (self.ledger_dir / "main.bean")).resolve()
        if not root.is_file():
            raise KernelError(LEDGER_LOAD_FAILED, f"ledger root file not found: {root}")
        try:
            entries, errors, options = loader.load_file(str(root))
        except Exception as exc:  # beancount may raise on I/O or plugin errors
            raise KernelError(
                LEDGER_LOAD_FAILED,
                f"Beancount failed to load {root.name}: {exc}",
                {"error": str(exc), "root": str(root)},
            ) from exc
        include_files, closure_source = self._closure(root, entries, options)
        manifest = manifest_for(self.ledger_dir, include_files)
        engine = engine_fingerprint()
        revision = revision_of(manifest, engine)
        accounts = self._accounts(entries)
        options = dict(options)
        options["_kernel_closure_source"] = closure_source
        return LoadResult(
            root_file=root,
            ledger_dir=self.ledger_dir,
            entries=entries,
            errors=list(errors),
            options=options,
            include_files=include_files,
            manifest=manifest,
            engine_fingerprint=engine,
            revision=revision,
            accounts=accounts,
        )

    def _closure(self, root: Path, entries: Sequence[Any], options: Mapping[str, Any]) -> tuple[list[Path], str]:
        """Discover the real input file set and cross-check it.

        Beancount 3.2.3 exposes the include closure in ``options_map["include"]``.
        Because that field is not a documented fourth return value, we verify it
        three ways: the root must be present, every entry's source file must be
        inside the closure, and every closure path must exist inside the ledger
        directory. A missing ``include`` key falls back to entry metadata + root,
        which is weaker; the mode is recorded in the load result.
        """
        source = "options.include"
        raw_include = options.get("include")
        paths: list[Path] = []
        if isinstance(raw_include, (list, tuple)) and raw_include:
            paths = [Path(str(item)).resolve() for item in raw_include]
        else:
            source = "entry_meta_fallback"
            seen: set[Path] = {root}
            for entry in entries:
                meta = getattr(entry, "meta", None) or {}
                filename = meta.get("filename")
                if isinstance(filename, str) and filename and filename != "<string>":
                    seen.add(Path(filename).resolve())
            paths = sorted(seen)

        if root not in paths:
            paths.append(root)
        unique: list[Path] = []
        for path in paths:
            if path in unique:
                continue
            if not path.is_file():
                raise KernelError(
                    CLOSURE_INCONSISTENT,
                    f"included ledger file is missing: {path}",
                    {"path": str(path)},
                )
            if not is_within(self.ledger_dir, path):
                raise KernelError(
                    CLOSURE_INCONSISTENT,
                    f"included ledger file is outside the ledger directory: {path}",
                    {"path": str(path), "ledger_dir": str(self.ledger_dir)},
                )
            unique.append(path)

        closure = set(unique)
        for entry in entries:
            meta = getattr(entry, "meta", None) or {}
            filename = meta.get("filename")
            if not isinstance(filename, str) or not filename or filename.startswith("<"):
                continue
            candidate = Path(filename)
            if not candidate.exists():
                # Plugins may synthesize entries with a pseudo filename (for
                # example beancount.plugins.auto_accounts). They are not input
                # files and cannot be part of the revision closure.
                continue
            candidate = candidate.resolve()
            if candidate not in closure:
                raise KernelError(
                    CLOSURE_INCONSISTENT,
                    f"entry source is not in the discovered include closure: {candidate}",
                    {"path": str(candidate), "source": source},
                )
        return sorted(unique), source

    @staticmethod
    def _accounts(entries: Sequence[Any]) -> dict[str, AccountInfo]:
        opens: dict[str, Any] = {}
        closes: dict[str, Any] = {}
        for entry in entries:
            if isinstance(entry, data.Open):
                opens.setdefault(entry.account, entry)
            elif isinstance(entry, data.Close):
                closes[entry.account] = entry
        result: dict[str, AccountInfo] = {}
        for name in sorted(set(opens) | set(closes)):
            open_entry = opens.get(name)
            close_entry = closes.get(name)
            meta = getattr(open_entry or close_entry, "meta", None) or {}
            currencies = tuple(open_entry.currencies or ()) if open_entry else ()
            booking = str(open_entry.booking) if open_entry and open_entry.booking is not None else None
            result[name] = AccountInfo(
                name=name,
                open_date=open_entry.date if open_entry else None,
                close_date=close_entry.date if close_entry else None,
                currencies=currencies,
                booking=booking,
                source_file=_relative_source(meta.get("filename")),
                source_line=meta.get("lineno"),
            )
        return result

    # -------------------------------------------------------------- semantics
    @staticmethod
    def canonical_value(value: Any) -> Any:
        if isinstance(value, Amount):
            return ("Amount", value.number, value.currency)
        if isinstance(value, Cost):
            return ("Cost", value.number, value.currency, value.date, value.label)
        if isinstance(value, enum.Enum):
            return ("Enum", type(value).__name__, str(value))
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (str, int, bool, dt.date, dt.datetime, type(None))):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return tuple(
                sorted(
                    ((BeancountAdapter.canonical_value(k), BeancountAdapter.canonical_value(v)) for k, v in value.items()),
                    key=repr,
                )
            )
        # Namedtuples are tuples: check ``_fields`` before the generic tuple
        # branch, otherwise their ``meta`` would leak into the identity.
        fields = getattr(value, "_fields", None)
        if fields:
            return (type(value).__name__,) + tuple(
                BeancountAdapter.canonical_value(getattr(value, name))
                for name in fields
                if name != "meta"
            )
        if isinstance(value, (list, tuple)):
            return tuple(BeancountAdapter.canonical_value(item) for item in value)
        if isinstance(value, (set, frozenset)):
            return tuple(sorted((BeancountAdapter.canonical_value(item) for item in value), key=repr))
        return ("Object", type(value).__name__, str(value))

    @classmethod
    def canonical_entry(cls, entry: Any, ledger_dir: Path | None = None) -> tuple:
        # ``Document.filename`` is an absolute path resolved by Beancount. In a
        # temp workspace that path changes even though the ledger content did
        # not, so identity uses the path relative to the ledger directory.
        if isinstance(entry, data.Document) and ledger_dir is not None:
            filename = entry.filename
            try:
                # ``abspath`` must not follow symlinks: the dry-run mirrors
                # non-ledger directories with symlinks, and following them
                # would turn the temp path back into the real ledger path.
                filename = (
                    Path(os.path.abspath(str(filename)))
                    .relative_to(Path(os.path.abspath(str(ledger_dir))))
                    .as_posix()
                )
            except (ValueError, OSError):
                filename = str(filename)
            return (
                "Document",
                cls.canonical_value(entry.date),
                cls.canonical_value(entry.account),
                filename,
                cls.canonical_value(entry.tags),
                cls.canonical_value(entry.links),
            )
        return cls.canonical_value(entry)

    @classmethod
    def entry_counter(cls, entries: Iterable[Any], ledger_dir: Path | None = None) -> Counter:
        return Counter(cls.canonical_entry(entry, ledger_dir) for entry in entries)

    @classmethod
    def options_signature(cls, options: Mapping[str, Any]) -> dict[str, Any]:
        signature: dict[str, Any] = {}
        for key in sorted(options):
            if key in _VOLATILE_OPTION_KEYS or key.startswith("_kernel_"):
                continue
            signature[key] = cls.canonical_value(options[key])
        return signature

    def balances(self, load: LoadResult, as_of: dt.date | None = None) -> dict[tuple[str, str], Decimal]:
        result: dict[tuple[str, str], Decimal] = {}
        for entry in load.entries:
            if as_of is not None and entry.date > as_of:
                continue
            if isinstance(entry, data.Transaction):
                for posting in entry.postings:
                    key = (posting.account, posting.units.currency)
                    result[key] = result.get(key, Decimal("0")) + posting.units.number
        return result

    def account_balances(self, load: LoadResult, account: str, as_of: dt.date | None = None) -> dict[str, Decimal]:
        return {
            currency: amount
            for (name, currency), amount in self.balances(load, as_of).items()
            if name == account
        }

    def last_posting_dates(self, load: LoadResult) -> dict[str, dt.date]:
        result: dict[str, dt.date] = {}
        for entry in load.entries:
            if isinstance(entry, data.Transaction):
                for posting in entry.postings:
                    previous = result.get(posting.account)
                    if previous is None or entry.date > previous:
                        result[posting.account] = entry.date
        return result

    def last_balance_assertions(self, load: LoadResult) -> dict[str, dict[str, Any]]:
        """Latest Beancount ``balance`` directive per account and currency.

        This is a *presence* summary only: Beancount itself decides whether the
        assertion passes, and a failure is reported as a global ledger error. The
        account view must not pretend an assertion passed.
        """
        latest: dict[str, dict[str, Any]] = {}
        for entry in load.entries:
            if not isinstance(entry, data.Balance):
                continue
            amount = entry.amount
            if amount is None:  # pragma: no cover - parser already rejects this
                continue
            currency = str(amount.currency)
            row = latest.get(entry.account)
            if row is None:
                row = {"date": entry.date, "amounts": {}, "currency_dates": {}, "meta": entry.meta}
                latest[entry.account] = row
            if entry.date > row["date"]:
                row["date"] = entry.date
            previous_currency_date = row["currency_dates"].get(currency)
            if previous_currency_date is None or entry.date >= previous_currency_date:
                row["currency_dates"][currency] = entry.date
                row["amounts"][currency] = format(amount.number, "f")
        records: dict[str, dict[str, Any]] = {}
        for account, row in latest.items():
            meta = row["meta"] or {}
            records[account] = {
                "date": row["date"].isoformat(),
                "amounts": dict(sorted(row["amounts"].items())),
                "source_file": _relative_source(meta.get("filename")),
                "source_line": meta.get("lineno"),
            }
        return records

    @staticmethod
    def error_records(errors: Sequence[Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for error in errors:
            source = getattr(error, "source", None) or {}
            filename = source.get("filename")
            records.append(
                {
                    "type": type(error).__name__,
                    "message": str(getattr(error, "message", error)),
                    "source_file": _relative_source(filename),
                    "lineno": source.get("lineno"),
                }
            )
        return records

    @staticmethod
    def error_identity(errors: Sequence[Any]) -> Counter:
        return Counter(
            (
                type(error).__name__,
                str(getattr(error, "message", error)),
                _relative_source((getattr(error, "source", None) or {}).get("filename")),
                (getattr(error, "source", None) or {}).get("lineno"),
            )
            for error in errors
        )


def _relative_source(filename: Any) -> str | None:
    if not isinstance(filename, str) or not filename or filename == "<string>":
        return None
    return os.path.basename(filename)


def target_hash(manifest: Mapping[str, str], write_target: str) -> str | None:
    return manifest.get(Path(write_target).as_posix())


def target_relative(ledger_dir: Path, path: Path) -> str:
    return relative_posix(ledger_dir, path)


def compute_revision(manifest: Mapping[str, str]) -> str:
    return revision_of(manifest)


def file_hash(path: Path) -> str:
    return sha256_file(path)
