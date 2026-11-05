# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The paths live processes on this host name, for passes that must never remove one.

Two readers. Where ``/proc`` exists (Linux) it is read directly: each process's command line,
environment, working directory, root, executable and open file descriptors. A macOS host has no
``/proc``, so there the same facts come from ``lsof`` (open files, working directories and
executables) and ``ps`` (command lines with their environments). Items another user's process
hides are skipped; a table that cannot be read at all returns ``None``, and a caller treats that
as "every path may be in use" and removes nothing.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

# argv -> (exit code, stdout), or None when the command cannot be run at all.
CommandRunner = Callable[[Sequence[str]], tuple[int, str] | None]

LSOF_ARGV: tuple[str, ...] = ("lsof", "-n", "-P", "-w", "-F", "n")
PS_ARGV: tuple[str, ...] = ("ps", "-axww", "-E", "-o", "command=")
_TIMEOUT_S = 300


def run_command(argv: Sequence[str]) -> tuple[int, str] | None:
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode, done.stdout


def read_proc_references(proc_root: Path = Path("/proc")) -> list[str] | None:
    """Unreadable items belong to other users; a missing process table returns None."""
    if not (proc_root / "self").is_dir():
        return None
    references: list[str] = []
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        return None
    for process in processes:
        if not process.name.isdigit() or not process.is_dir():
            continue
        for name in ("cmdline", "environ"):
            with suppress(OSError):
                references.extend(
                    item.decode(errors="replace")
                    for item in (process / name).read_bytes().split(b"\0")
                    if item
                )
        links = [process / name for name in ("cwd", "root", "exe")]
        with suppress(OSError):
            links.extend((process / "fd").iterdir())
        for link in links:
            with suppress(OSError):
                references.append(os.readlink(link))
    return references


def read_lsof_references(run: CommandRunner = run_command) -> list[str] | None:
    """The macOS table: lsof's name fields and ps's command lines with environments.

    lsof exits 1 when some process could not be read (another user's) but still lists the
    rest, so its output decides, not its exit code: no name field at all means no table.
    ps must succeed and list at least one process.
    """
    lsof = run(LSOF_ARGV)
    if lsof is None:
        return None
    names = [line[1:] for line in lsof[1].splitlines() if line.startswith("n")]
    if not names:
        return None
    ps = run(PS_ARGV)
    if ps is None or ps[0] != 0:
        return None
    commands = [line for line in ps[1].splitlines() if line.strip()]
    if not commands:
        return None
    return names + commands


def read_process_references(
    proc_root: Path = Path("/proc"), *, run: CommandRunner = run_command
) -> list[str] | None:
    """``/proc`` where the host has one, lsof and ps where it does not."""
    if (proc_root / "self").is_dir():
        return read_proc_references(proc_root)
    return read_lsof_references(run)


def path_referenced(path: Path, references: Sequence[str]) -> bool:
    """True when a reference names ``path`` or anything below it, by its own or resolved path.

    A longer sibling name (``lane-1`` against ``lane-10``) does not match, nor does the path
    appearing as the tail of another path.
    """
    candidates = {str(path)}
    with suppress(OSError):
        candidates.add(str(path.resolve()))
    for base in candidates:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_.\-/])" + re.escape(base) + r"(?![A-Za-z0-9_.\-])"
        )
        if any(pattern.search(reference) for reference in references):
            return True
    return False
