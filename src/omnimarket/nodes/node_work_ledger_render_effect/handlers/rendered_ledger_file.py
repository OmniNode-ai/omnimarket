# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rendered markdown file: its own watermark, its lock and its torn tail (OMN-20275).

The file holds a baseline line, then one complete line per row in ``ledger_seq`` order.
The watermark W is the baseline plus the count of complete rows, read from the file on
recovery; a second file holding W would render a row twice after a crash between the two
writes (tla/ledger_projection, mutation sidecar_watermark). A torn last line (no
terminating newline) is truncated before W is read. One node owns one file: an exclusive
lock beside it refuses a second node, as the serve process does for the ledger of record.
"""

from __future__ import annotations

import fcntl
import os
import re
from pathlib import Path

from omnimarket.events.model_ledger_row_event import work_ledger_row_id

_BASELINE = re.compile(r"^<!-- work-ledger-projection baseline_seq=(\d+) -->$")


class WorkLedgerRenderLockHeldError(RuntimeError):
    """Another node holds the exclusive lock beside this file."""


def baseline_line(baseline_seq: int) -> str:
    return f"<!-- work-ledger-projection baseline_seq={baseline_seq} -->"


def create_baseline_file(path: Path, *, baseline_seq: int) -> None:
    """Start a projection file whose history up to ``baseline_seq`` was copied once."""
    if baseline_seq < 0:
        raise ValueError("baseline_seq must not be negative")
    with path.open("xb") as handle:
        handle.write((baseline_line(baseline_seq) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


class RenderedLedgerFile:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock_fd: int | None = None
        self._fd: int | None = None
        self.baseline_seq = 0
        self.row_ids: list[str] = []

    @property
    def watermark(self) -> int:
        return self.baseline_seq + len(self.row_ids)

    def recover(self) -> None:
        """Take the lock, truncate a torn tail and read the baseline and the rows."""
        self._acquire_lock()
        try:
            self._fd = os.open(self._path, os.O_RDWR | os.O_APPEND)
            data = _read_all(self._fd)
            end = data.rfind(b"\n") + 1
            if end != len(data):
                os.ftruncate(self._fd, end)
                os.fsync(self._fd)
                data = data[:end]
            lines = data.decode("utf-8").split("\n")[:-1]
            match = _BASELINE.match(lines[0]) if lines else None
            if match is None:
                raise ValueError(
                    f"{self._path} has no baseline line (a complete first line "
                    "'<!-- work-ledger-projection baseline_seq=N -->')"
                )
            self.baseline_seq = int(match.group(1))
            self.row_ids = [work_ledger_row_id(line) for line in lines[1:]]
        except BaseException:
            self.close()
            raise

    def append(self, raw_row: str) -> None:
        """Write one complete row line and fsync it; the row is rendered once it returns."""
        if self._fd is None:
            raise RuntimeError("recover() before append()")
        os.write(self._fd, (raw_row + "\n").encode("utf-8"))
        os.fsync(self._fd)
        self.row_ids.append(work_ledger_row_id(raw_row))

    def close(self) -> None:
        for name in ("_fd", "_lock_fd"):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)  # closing the lock fd releases the flock
                setattr(self, name, None)

    def _acquire_lock(self) -> None:
        lock_path = self._path.with_name(self._path.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise WorkLedgerRenderLockHeldError(
                f"another node holds {lock_path}; one node owns one file"
            ) from exc
        self._lock_fd = fd


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(fd, 1 << 20):
        chunks.append(chunk)
    return b"".join(chunks)


__all__: list[str] = [
    "RenderedLedgerFile",
    "WorkLedgerRenderLockHeldError",
    "baseline_line",
    "create_baseline_file",
]
