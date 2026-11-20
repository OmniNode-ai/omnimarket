# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read and unset ``core.worktree`` through ``git config --file``, never ``git -C``.

``git -C <clone>`` makes git resolve the clone's configured worktree first, and exits 128 when that
directory is gone, so the value could be neither read nor unset that way. ``--file`` reads the
config text only.
"""

import os
import subprocess
import sys
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

KEY = "core.worktree"
TIMEOUT_SECONDS = 30


def _config(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "config", "--file", str(path / ".git" / "config"), *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
        env=scrub_git_location_env(os.environ),
    )


class GitConfigFileCoreWorktree:
    def read(self, path: Path) -> str | None:
        """The raw value, or None when the key is unset. A git failure other than unset raises."""
        proc = _config(path, "--get", KEY)
        if proc.returncode == 1:
            return None
        if proc.returncode != 0:
            raise RuntimeError(f"git config --get failed ({proc.returncode})")
        value = proc.stdout.strip()
        return value or None

    def unset(self, path: Path) -> None:
        proc = _config(path, "--unset", KEY)
        if proc.returncode != 0:
            raise RuntimeError(f"git config --unset failed ({proc.returncode})")


class CommandStatusRowWriter:
    """Append one row through a declared command, never by writing a file here; ``{row}`` in the argv is the row."""

    def __init__(self, argv: list[str]) -> None:
        if not any("{row}" in token for token in argv):
            raise ValueError("the append command must carry the {row} token")
        self._argv = argv

    def write(self, row: str) -> None:
        argv = [token.replace("{row}", row) for token in self._argv]
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=120, check=False
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr[-300:])
            raise RuntimeError(f"ledger append failed ({proc.returncode})")
