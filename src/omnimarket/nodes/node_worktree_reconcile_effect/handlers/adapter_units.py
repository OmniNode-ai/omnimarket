# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read what the installed systemd units run from (OMN-22736).

``systemctl list-unit-files`` names every service the user manager and the system manager know,
``systemctl cat`` prints each definition with its drop-ins, and the environment files a unit names
are read as well. Nothing here guesses: a manager that cannot be asked, a command that fails or a
required environment file that cannot be read raises :class:`UnitSnapshotError`, and the facts probe
turns that into a probe error, which keeps every tree.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.worktree_reconcile.unit_roots import (
    UnitRoots,
    environment_file_paths,
    environment_files,
    split_systemctl_cat,
    unit_paths,
)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

#: The service managers asked, as the flag `systemctl` takes.
SCOPES: tuple[str, ...] = ("--user", "--system")
_TIMEOUT_SECONDS = 120.0


class UnitSnapshotError(RuntimeError):
    """The installed units cannot all be read, so no tree may be judged free of them."""


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnitSnapshotError(f"{command[0]}: {type(exc).__name__}") from exc


def _read_environment_file(path: str, optional: bool) -> frozenset[str]:
    try:
        return environment_file_paths(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if optional:
            return frozenset()
        raise UnitSnapshotError(f"environment file {path} is missing") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise UnitSnapshotError(
            f"environment file {path}: {type(exc).__name__}"
        ) from exc


class SystemdUnitRootReader:
    """The absolute paths every installed service names, with one unit that names each."""

    def __init__(
        self, *, run: Runner = run_command, scopes: Sequence[str] = SCOPES
    ) -> None:
        self._run = run
        self._scopes = tuple(scopes)

    def read(self) -> UnitRoots:
        per_unit: dict[str, set[str]] = {}
        for scope in self._scopes:
            listing = self._run(
                [
                    "systemctl",
                    scope,
                    "list-unit-files",
                    "--type=service",
                    "--no-legend",
                    "--plain",
                ]
            )
            if listing.returncode:
                raise UnitSnapshotError(
                    f"systemctl {scope} list-unit-files exited {listing.returncode}"
                )
            names = [
                line.split()[0] for line in listing.stdout.splitlines() if line.strip()
            ]
            if not names:
                continue
            cat = self._run(["systemctl", scope, "cat", *names])
            if cat.returncode:
                raise UnitSnapshotError(
                    f"systemctl {scope} cat exited {cat.returncode}"
                )
            units = split_systemctl_cat(cat.stdout)
            if not units:
                raise UnitSnapshotError(f"systemctl {scope} cat printed no unit")
            for name, text in units.items():
                paths = per_unit.setdefault(name, set())
                paths |= unit_paths(text)
                for file, optional in environment_files(text):
                    paths |= _read_environment_file(file, optional)
        return UnitRoots.from_paths(per_unit)
