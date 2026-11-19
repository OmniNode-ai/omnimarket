# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read what the installed systemd units run from (OMN-22736).

``systemctl list-unit-files`` names every service the user manager and the system manager know,
``systemctl cat`` prints each definition with its drop-ins, and the environment files a unit names
are read as well. Nothing here guesses: a manager that cannot be asked, a command that fails or a
required environment file that cannot be read raises :class:`UnitSnapshotError`, and the facts probe
turns that into a probe error, which keeps every tree. Two exceptions: a file the run user
cannot open falls back to the paths the host overlay declared it holds, and a transient unit that
is not running, runs from no worktree and names a required environment file that is gone is skipped
with a typed reason (:class:`ModelSkippedUnit`) and the scan goes on (OMN-24901).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from omnimarket.handlers.rules_unit_roots import (
    UnitRoots,
    environment_file_paths,
    environment_files,
    split_systemctl_cat,
    under_worktrees,
    unit_paths,
)
from omnimarket.models.model_skipped_unit import ModelSkippedUnit

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

#: The service managers asked, as the flag `systemctl` takes.
SCOPES: tuple[str, ...] = ("--user", "--system")
_TIMEOUT_SECONDS = 120.0
#: The state ``systemctl list-unit-files`` prints for a unit created through the manager's API.
_TRANSIENT = "transient"
#: ActiveState values of a unit that is not running and is not about to run.
_NOT_RUNNING = frozenset({"failed", "inactive"})


class UnitSnapshotError(RuntimeError):
    """The installed units cannot all be read, so no tree may be judged free of them."""


class EnvironmentFileMissingError(UnitSnapshotError):
    """A required environment file of a unit does not exist."""

    def __init__(self, path: str) -> None:
        super().__init__(f"environment file {path} is missing")
        self.path = path


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


def _read_environment_file(
    path: str,
    optional: bool,
    declared: Mapping[str, Sequence[str]] | None = None,
    read_file: Callable[[str], str] | None = None,
) -> frozenset[str]:
    try:
        text = (
            read_file(path)
            if read_file is not None
            else Path(path).read_text(encoding="utf-8")
        )
        return environment_file_paths(text)
    except FileNotFoundError:
        if optional:
            return frozenset()
        raise EnvironmentFileMissingError(path) from None
    except PermissionError:
        # A file the run user cannot open: use the paths the host overlay declared it holds.
        if declared is not None and path in declared:
            return frozenset(p.rstrip("/") for p in declared[path])
        raise UnitSnapshotError(f"environment file {path}: PermissionError") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise UnitSnapshotError(
            f"environment file {path}: {type(exc).__name__}"
        ) from exc


class SystemdUnitRootReader:
    """The absolute paths every installed service names, with one unit that names each."""

    def __init__(
        self,
        *,
        run: Runner = run_command,
        scopes: Sequence[str] = SCOPES,
        declared_environment_files: Mapping[str, Sequence[str]] | None = None,
        read_file: Callable[[str], str] | None = None,
    ) -> None:
        self._run = run
        self._scopes = tuple(scopes)
        self._declared = declared_environment_files
        self._read_file = read_file

    def _skippable(
        self, scope: str, unit: str, state: str, paths: set[str], missing: str
    ) -> bool:
        """True for a transient unit that is not running and names no worktree.

        An installed unit, an active or starting unit and a unit that runs from (or whose missing
        file lies in) a worktree are never skipped: they are the cases the snapshot must refuse.
        """
        if state != _TRANSIENT or any(under_worktrees(p) for p in (*paths, missing)):
            return False
        shown = self._run(
            ["systemctl", scope, "show", unit, "--property=ActiveState", "--value"]
        )
        if shown.returncode:
            raise UnitSnapshotError(
                f"systemctl {scope} show {unit} exited {shown.returncode}"
            )
        return shown.stdout.strip() in _NOT_RUNNING

    def read(self) -> UnitRoots:
        per_unit: dict[str, set[str]] = {}
        skipped: list[ModelSkippedUnit] = []
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
            rows = [
                line.split() for line in listing.stdout.splitlines() if line.strip()
            ]
            names = [row[0] for row in rows]
            states = {row[0]: row[1] if len(row) > 1 else "" for row in rows}
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
                found = set(unit_paths(text))
                try:
                    for file, optional in environment_files(text):
                        found |= _read_environment_file(
                            file, optional, self._declared, self._read_file
                        )
                except EnvironmentFileMissingError as gone:
                    if not self._skippable(
                        scope, name, states.get(name, ""), found, gone.path
                    ):
                        raise
                    skipped.append(
                        ModelSkippedUnit(
                            unit=name,
                            reason="environment_file_missing",
                            path=gone.path,
                        )
                    )
                    continue
                per_unit.setdefault(name, set()).update(found)
        return UnitRoots.from_paths(per_unit, skipped)
