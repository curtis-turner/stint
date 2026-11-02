"""Advisory cross-process lock on a state file.

Implementation: atomic creation of ``<state>.lock`` containing the holder's
PID, the acquired-at timestamp, and a random ownership token.
``os.open(..., O_CREAT|O_EXCL)`` is atomic on POSIX and on Windows for local
filesystems, so two processes racing for the same lock have exactly one
winner. The winner keeps the O_EXCL file descriptor open for the life of the
lock and uses it as the ownership proof: ``release()`` first verifies that
the on-disk lock file is still the one *this* holder created (same
device/inode pair as the retained fd) before unlinking. So the documented
stale-lock workflow — the operator deletes a stale ``<state>.lock``, and a
new holder acquires — cannot be broken by the old holder's waking
``release()`` deleting the *new* holder's lock. Likewise, a failed write in
``acquire()`` removes the fresh (empty) lock file instead of stranding a
lockout that only manual deletion can clear.

Limitations:
  - Stale locks (holder crashed) are not detected automatically. The error
    message includes the holding PID so the user can resolve manually
    (``rm state.yaml.lock``).
  - Not safe across NFS without leases; the typical state file lives in a
    local git checkout, which is fine.
"""

from __future__ import annotations

import os
import uuid
from contextlib import suppress
from datetime import datetime, timezone
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
            fd = os.open(
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
        token = uuid.uuid4().hex
        body = (
            f"pid={os.getpid()}\n"
            f"acquired_at={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
            f"token={token}\n"
        ).encode()
        try:
            os.write(fd, body)
            os.fsync(fd)
        except OSError:
            # A stranded empty lock file means every future acquire fails
            # with "locked" until someone deletes the file by hand. Remove
            # the just-created file: the O_EXCL creation that produced it
            # is ours by definition, so a best-effort unlink is safe.
            os.close(fd)
            with suppress(OSError):
                os.unlink(self.lock_path)
            raise
        self._fd = fd  # kept open as the ownership proof for release()

    def release(self) -> None:
        """Remove the lock file — but only if it is still ours.

        The on-disk file may no longer be ours: the operator can delete a
        lock they believe is stale, and a new holder then acquires. Unlinking
        unconditionally would let the waking old holder destroy the new
        holder's lock and let a third process acquire while the new holder
        is still writing. So verify that the lock file is the same inode as
        the O_EXCL fd this holder created before removing it. If the file is
        gone, or is a different holder's, leave it alone.
        """
        fd = self._fd
        self._fd = None
        if fd is None:
            # Never acquired (or already released): nothing we can prove
            # ownership of, so nothing to remove.
            return
        ours = _lock_file_is_ours(self.lock_path, fd)
        with suppress(OSError):
            os.close(fd)
        if ours:
            with suppress(OSError):
                os.unlink(self.lock_path)

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


def _lock_file_is_ours(lock_path: Path, fd: int) -> bool:
    """True if the on-disk lock file is the one this holder created.

    Compares the device/inode pair of the on-disk file against the retained
    O_EXCL fd. A replacement by another holder (or the operator's manual
    delete + re-create) always yields a different inode, so this closes the
    delete-and-replace race between "check it is mine" and "unlink it".
    ``st_ino``/``st_dev`` are populated on Windows for local files too.
    """
    try:
        disk = os.stat(lock_path)
        handle = os.fstat(fd)
        return (disk.st_dev, disk.st_ino) == (handle.st_dev, handle.st_ino)
    except OSError:
        return False


def _read_holder(lock_path: Path) -> str:
    try:
        return lock_path.read_text(encoding="utf-8").strip().replace("\n", "; ")
    except OSError:
        return "(unknown — cannot read lock file)"
