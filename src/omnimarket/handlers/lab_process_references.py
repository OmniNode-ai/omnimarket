# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host process references used by lab artifact retention; unknown means keep."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from contextlib import suppress
from pathlib import Path


def _mac_process_references() -> list[str] | None:
    """lsof covers open files and cwd; ps also covers plugin arguments and environment."""
    try:
        opened = subprocess.run(
            ["lsof", "-nP", "-F0n"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        commands = subprocess.run(
            ["ps", "eww", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if (
        opened.returncode
        or commands.returncode
        or opened.stderr.strip()
        or commands.stderr.strip()
        or not commands.stdout.strip()
    ):
        return None
    # -F0 separates fields with NULs and processes with newlines. Keep names
    # intact (including spaces), never parse lsof's human-readable columns.
    fields = [field.lstrip("\n") for field in opened.stdout.split("\0")]
    paths = [field[1:] for field in fields if field.startswith("n/")]
    if not paths or not any(re.fullmatch(r"p[0-9]+", field) for field in fields):
        return None
    return paths + commands.stdout.splitlines()


def read_process_references(proc_root: Path = Path("/proc")) -> list[str] | None:
    """Read the host's process table; unavailable evidence blocks destructive cleanup."""
    if sys.platform == "darwin":
        return _mac_process_references()
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


def path_is_referenced(tree: Path, references: list[str]) -> bool:
    """Match a tree or a descendant, including arguments and resolved path aliases."""
    for base in {str(tree), str(tree.resolve())}:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_.\-/])" + re.escape(base) + r"(?![A-Za-z0-9_.\-])"
        )
        if any(pattern.search(reference) for reference in references):
            return True
    return False
