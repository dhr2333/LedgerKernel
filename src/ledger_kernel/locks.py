"""OS-level locks used to serialize kernel writers.

``flock`` coordinates kernel instances only. Editors do not honor this lock;
the kernel therefore re-checks hashes immediately before replacing a file.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from .errors import KERNEL_BUSY, KernelError


class FileLock:
    def __init__(self, path: Path, *, timeout_message: str | None = None):
        self.path = path
        self._fd: int | None = None
        self._message = timeout_message or f"another kernel instance holds {path.name}"

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise KernelError(
                KERNEL_BUSY,
                self._message,
                {"lock": str(self.path), "error": str(exc)},
            ) from exc
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def lock_is_held(path: Path) -> bool:
    """Best-effort probe used by tests and status reporting."""
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
