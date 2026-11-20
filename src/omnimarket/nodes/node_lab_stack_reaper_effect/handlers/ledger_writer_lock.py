# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger writer's mutex, taken by the read-only snapshot.

Every writer of a ledger takes one exclusive flock on one lock file whose path is a function of
the resolved ledger path alone. The snapshot takes the same lock so a concurrent archive roll
cannot move rows between the live file and an archive while the snapshot reads both. The lock
file is never deleted: a holder that dies releases it with its last descriptor.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

LOCK_DIRNAME = ".ledger_locks"
LOCK_FILE_SUFFIX = ".flock"
LOCK_DIR_GITIGNORE = "# ledger lock files, never tracked\n*\n"
POLL_SECONDS = 0.5


def lock_path_for(ledger: Path) -> Path:
    """The one lock file every writer of `ledger` takes."""
    resolved = ledger.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:24]
    return (
        resolved.parent / LOCK_DIRNAME / f"{resolved.name}.{digest}{LOCK_FILE_SUFFIX}"
    )


class LedgerWriterLock:
    """An exclusive flock on the ledger's lock file, bounded by a timeout in seconds."""

    def __init__(self, ledger: Path, timeout: float) -> None:
        self.ledger = ledger
        self.timeout = timeout
        self.lock_file = lock_path_for(ledger)
        self._fd: int | None = None

    def _is_current_lock_file(self, fd: int) -> bool:
        try:
            on_disk = self.lock_file.stat()
        except FileNotFoundError:
            return False
        held = os.fstat(fd)
        return (on_disk.st_dev, on_disk.st_ino) == (held.st_dev, held.st_ino)

    def _ensure_directory(self) -> None:
        directory = self.lock_file.parent
        directory.mkdir(parents=True, exist_ok=True)
        ignore = directory / ".gitignore"
        if not ignore.exists():
            with (
                suppress(FileExistsError),
                ignore.open("x", encoding="utf-8") as handle,
            ):
                handle.write(LOCK_DIR_GITIGNORE)

    def acquire(self) -> None:
        self._ensure_directory()
        deadline = time.monotonic() + self.timeout
        while True:
            fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                if self.timeout == 0 or time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for {self.ledger}; lock={self.lock_file}"
                    ) from None
                time.sleep(POLL_SECONDS)
                continue
            except BaseException:
                os.close(fd)
                raise
            if not self._is_current_lock_file(fd):
                os.close(fd)
                continue
            self._fd = fd
            try:
                holder = json.dumps(
                    {
                        "ledger": str(self.ledger),
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "command": ["lab-stack-reaper", "snapshot"],
                        "acquired_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                    sort_keys=True,
                )
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, (holder + "\n").encode("utf-8"))
            except BaseException:
                self.release()
                raise
            return

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> LedgerWriterLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
