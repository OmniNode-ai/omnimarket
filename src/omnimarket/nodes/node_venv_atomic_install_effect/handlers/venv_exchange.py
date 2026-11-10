# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exchange two paths in one syscall, and the journal that undoes an exchange."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

RENAME_EXCHANGE = 2  # renameat2 on Linux
RENAME_SWAP = 2  # renamex_np on macOS
AT_FDCWD = -100

Exchange = Callable[[Path, Path], None]


def exchange_paths(first: Path, second: Path) -> None:
    """Swap two paths atomically; fall back to three renames where the OS has no exchange."""
    a, b = os.fsencode(first), os.fsencode(second)
    libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    if sys.platform == "darwin":
        done = libc.renamex_np(a, b, RENAME_SWAP) == 0
    elif hasattr(libc, "renameat2"):
        done = libc.renameat2(AT_FDCWD, a, AT_FDCWD, b, RENAME_EXCHANGE) == 0
    else:
        done = False
    if done:
        return
    parked = first.with_name(first.name + ".exchange-parked")
    first.rename(parked)
    try:
        second.rename(first)
    except OSError:
        parked.rename(first)
        raise
    parked.rename(second)


class Journal:
    """One JSON line per step, written and synced before the step runs."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, **entry: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def entries(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def roll_back(journal: Journal, exchange: Exchange) -> list[str]:
    """Undo every journalled step, newest first. Returns the unit keys restored."""
    restored: list[str] = []
    for entry in reversed(journal.entries()):
        op = entry["op"]
        live, stage = (
            Path(str(entry.get("live", ""))),
            Path(str(entry.get("stage", ""))),
        )
        if op == "exchange":
            # The old unit's inode is where it went: at the stage path once exchanged.
            if _exists(stage) and os.lstat(stage).st_ino == entry["live_ino"]:
                exchange(live, stage)
                restored.append(str(entry["unit"]))
        elif op == "add":
            if _exists(live) and not _exists(stage):
                stage.parent.mkdir(parents=True, exist_ok=True)
                live.rename(stage)
                restored.append(str(entry["unit"]))
        elif op == "remove":
            if _exists(stage) and not _exists(live):
                stage.rename(live)
                restored.append(str(entry["unit"]))
        elif op == "mkdir":
            made = Path(str(entry["path"]))
            if made.is_dir() and not any(made.iterdir()):
                made.rmdir()
    return restored
