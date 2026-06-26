"""Advisory cross-process lock on a state file.

Implementation: atomic creation of ``<state>.lock`` containing the holder's
PID + acquired-at timestamp. ``os.open(..., O_CREAT|O_EXCL)`` is atomic on
POSIX and on Windows for local filesystems, so two processes racing for the
same lock have exactly one winner.

Limitations:
  - Stale locks (holder crashed) are not detected automatically. The error
    message includes the holding PID so the user can resolve manually
    (``rm state.yaml.lock``).
  - Not safe across NFS without leases; the typical state file lives in a
    local git checkout, which is fine.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from stint.exceptions import StintError


class StateLockError(StintError):
    """Raised when the state file lock cannot be acquired."""


class StateLock:
    """Context manager. Acquire on enter, release on exit. ``acquire()`` /
    ``release()`` also exposed for explicit use."""

    def __init__(self, state_path: str | Path) -> None:
        self.state_path = Path(state_path)
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self._fd: int | None = None

    def acquire(self) -> None:
        try:
            self._fd = os.open(
                self.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            holder = _read_holder(self.lock_path)
            raise StateLockError(
                f"state file {self.state_path!s} is locked by {holder}. "
                f"If the holder is gone, delete {self.lock_path!s} manually."
            ) from None
        body = (f"pid={os.getpid()}\nacquired_at={datetime.now(UTC).isoformat(timespec='seconds')}\n").encode()
        try:
            os.write(self._fd, body)
        finally:
            os.close(self._fd)
            self._fd = None

    def release(self) -> None:
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> StateLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def _read_holder(lock_path: Path) -> str:
    try:
        return lock_path.read_text().strip().replace("\n", "; ")
    except OSError:
        return "(unknown — cannot read lock file)"
