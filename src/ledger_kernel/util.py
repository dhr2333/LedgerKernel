"""Small filesystem, hashing and text utilities.

Everything here is deliberately dependency-free so that domain modules can be
tested without a running kernel.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .errors import KernelError

UTC = _dt.timezone.utc


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(tz=UTC)


def now_iso() -> str:
    """UTC timestamp with a trailing ``Z`` (stable across platforms)."""
    return now_utc().isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_datetime(value: str) -> _dt.datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = _dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def new_id(prefix: str) -> str:
    """Short, sortable-ish identifier with a readable timestamp prefix."""
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}_{secrets.token_hex(4)}"


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Write bytes via a same-directory temp file and ``os.replace``."""
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        fsync_dir(path.parent)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def append_jsonl(path: Path, value: Any) -> None:
    ensure_dir(path.parent)
    line = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise KernelError(
                    "AUDIT_INTEGRITY",
                    f"audit line {lineno} is not valid JSON",
                    {"line": lineno, "error": str(exc)},
                ) from exc
    return rows


def is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def relative_posix(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def safe_join(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise KernelError("SCHEMA_INVALID", f"path must be relative: {relative!r}")
    candidate = (root / relative).resolve()
    if not is_within(root, candidate):
        raise KernelError("SCHEMA_INVALID", f"path escapes ledger directory: {relative!r}")
    return candidate


@dataclass(frozen=True)
class TextFile:
    text: str
    encoding: str
    newline: str
    bom: bool


def read_text_file(path: Path) -> TextFile:
    data = path.read_bytes()
    bom = data.startswith(b"\xef\xbb\xbf")
    if bom:
        data = data[3:]
    try:
        text = data.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError as exc:
        raise KernelError(
            "CONFIG_INVALID",
            f"ledger file is not valid UTF-8: {path}",
            {"path": str(path), "error": str(exc)},
        ) from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    return TextFile(text=text, encoding=encoding, newline=newline, bom=bom)


def encode_text_file(text: str, info: TextFile) -> bytes:
    data = text.encode(info.encoding)
    if info.bom:
        data = b"\xef\xbb\xbf" + data
    return data


def append_fragment(existing: str, fragment: str, newline: str) -> str:
    """Append a rendered fragment while keeping a single blank separator line.

    ``fragment`` is expected to end with exactly one newline (the renderer
    guarantees this). Existing content style (LF/CRLF) is preserved.
    """
    if not fragment.endswith(newline):
        fragment = fragment + newline
    prefix = existing
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    if prefix and not prefix.endswith(newline + newline):
        prefix += newline
    return prefix + fragment


_AMOUNT_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def parse_amount(value: Any, field: str = "amount") -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise KernelError("SCHEMA_INVALID", f"{field} must be a decimal string, not a float")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not value:
        raise KernelError("SCHEMA_INVALID", f"{field} must be a non-empty decimal string")
    text = value.strip()
    if not _AMOUNT_RE.match(text):
        raise KernelError("SCHEMA_INVALID", f"{field} is not a plain decimal string: {value!r}")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise KernelError("SCHEMA_INVALID", f"{field} is not a valid decimal") from exc
    if not amount.is_finite():
        raise KernelError("SCHEMA_INVALID", f"{field} must be finite")
    if abs(amount) > Decimal("1000000000000"):
        raise KernelError("SCHEMA_INVALID", f"{field} is implausibly large")
    if -amount.as_tuple().exponent > 8:
        raise KernelError("SCHEMA_INVALID", f"{field} has more than 8 decimal places")
    return amount


def format_amount(value: Decimal) -> str:
    text = format(value, "f")
    if text == "-0":
        return "0"
    return text


def escape_display(value: str) -> str:
    """Escape untrusted text for single-line terminal/HTML-ish display."""
    out: list[str] = []
    for char in value:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif code in (0x2028, 0x2029) or 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


def truncate(value: str, limit: int = 120) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def iter_files(root: Path, skip_dirs: Iterable[str] = (".ledger-kernel", ".git")) -> Iterable[Path]:
    skip = set(skip_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in skip]
        for name in filenames:
            yield Path(dirpath) / name
