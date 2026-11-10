# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read what the installed and loaded launchd jobs run from (OMN-23323).

The systemd reader answers on a Linux host only; a Mac has none of ``systemctl``, so a reconcile
run there could never name a tree a unit runs from. On 2026-11-09 the factory floor publisher's
overlay root was a worktree, the worktree was removed and the published floor lost every owner
ticket added that day.

Two sources, both fail closed:

* every agent or daemon definition in the declared launchd directories, read with the same path
  rules as the systemd reader (a program or directory string is one literal path; an environment
  value is a path list split at its colons), and
* every job ``launchctl list`` reports as loaded whose definition is not among those files,
  read from ``launchctl print`` (a job bootstrapped from a plist kept elsewhere).

A command that fails, a definition that cannot be parsed or a loaded job that cannot be printed
raises :class:`UnitSnapshotError`, and the facts probe turns that into a probe error, which keeps
every tree.
"""

from __future__ import annotations

import os
import plistlib
import re
from collections.abc import Sequence
from pathlib import Path

from omnimarket.handlers.rules_unit_roots import UnitRoots, plist_paths
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_units import (
    Runner,
    UnitSnapshotError,
    run_command,
)

#: Where launchd agents and daemons are installed on a Mac; a directory the host lacks is skipped.
LAUNCHD_DIRECTORIES: tuple[str, ...] = (
    "~/Library/LaunchAgents",
    "/Library/LaunchAgents",
    "/Library/LaunchDaemons",
)
#: The operating system's own jobs name no checkout; reading each of them is wasted time.
SYSTEM_LABEL_PREFIX = "com.apple."
_ABSOLUTE_PATH = re.compile(r"(?<![\w.~/$])/[^\s:;'\"<>=,]+")


def print_paths(text: str) -> frozenset[str]:
    """Every absolute path in the output of ``launchctl print`` for one job."""
    return frozenset(
        path.rstrip("/") for path in _ABSOLUTE_PATH.findall(text) if len(path) > 1
    )


class LaunchdUnitRootReader:
    """The absolute paths every installed or loaded launchd job names, with one job that names each."""

    def __init__(
        self,
        *,
        directories: Sequence[str] = LAUNCHD_DIRECTORIES,
        run: Runner = run_command,
        uid: int | None = None,
    ) -> None:
        self._directories = tuple(Path(d).expanduser() for d in directories)
        self._run = run
        self._uid = os.getuid() if uid is None else uid

    def _loaded_labels(self) -> list[str]:
        listing = self._run(["launchctl", "list"])
        if listing.returncode:
            raise UnitSnapshotError(f"launchctl list exited {listing.returncode}")
        labels = [
            line.split()[-1]
            for line in listing.stdout.splitlines()[1:]
            if len(line.split()) >= 3
        ]
        if not labels:
            raise UnitSnapshotError("launchctl list printed no job")
        return labels

    def _installed(self) -> dict[str, set[str]]:
        per_unit: dict[str, set[str]] = {}
        for directory in self._directories:
            if not directory.is_dir():
                continue
            for entry in sorted(directory.glob("*.plist")):
                if entry.name.startswith("."):
                    continue
                try:
                    data = entry.read_bytes()
                    document = plistlib.loads(data)
                    paths = plist_paths(data)
                except (OSError, ValueError, plistlib.InvalidFileException) as exc:
                    raise UnitSnapshotError(f"{entry}: {type(exc).__name__}") from exc
                label = str(document.get("Label") or entry.stem)
                per_unit.setdefault(label, set()).update(paths)
        return per_unit

    def read(self) -> UnitRoots:
        loaded = self._loaded_labels()
        per_unit = self._installed()
        for label in loaded:
            if label in per_unit or label.startswith(SYSTEM_LABEL_PREFIX):
                continue
            printed = self._run(["launchctl", "print", f"gui/{self._uid}/{label}"])
            if printed.returncode:
                raise UnitSnapshotError(
                    f"launchctl print {label} exited {printed.returncode}"
                )
            per_unit[label] = set(print_paths(printed.stdout))
        return UnitRoots.from_paths(per_unit)
