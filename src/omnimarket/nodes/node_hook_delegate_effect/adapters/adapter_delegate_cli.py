# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ProtocolDelegateCli over a real process: the installed `onex`, run with a time limit."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from omnimarket.nodes.node_hook_delegate_effect.models import (
    EnumDelegateCliState,
    ModelDelegateCliRun,
)


def installed_onex() -> Path | None:
    """The `onex` installed beside this Python, else the first on PATH.

    The hook runs this Python (PLUGIN_PYTHON_BIN), so the onex beside it is the
    install the hook belongs to. PATH is Claude Code's, and can name another
    onex: seen 2026-11-13, a launcher whose older uv-tool onex knew no
    Reflection AI (OMN-23413).
    """
    beside = Path(sys.executable).parent / "onex"
    if beside.is_file():
        return beside
    found = shutil.which("onex")
    return Path(found) if found else None


class AdapterDelegateCli:
    """Runs `onex`; a missing `onex` or a start failure is reported, not raised."""

    def __init__(self, onex: Path | None = None) -> None:
        self._onex = onex

    def run(self, args: list[str], timeout_seconds: float) -> ModelDelegateCliRun:
        onex = self._onex or installed_onex()
        if onex is None or not onex.is_file():
            return ModelDelegateCliRun(state=EnumDelegateCliState.NOT_FOUND)
        try:
            proc = subprocess.run(
                [str(onex), *args],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ModelDelegateCliRun(state=EnumDelegateCliState.TIMED_OUT)
        except OSError:
            return ModelDelegateCliRun(state=EnumDelegateCliState.NOT_STARTED)
        return ModelDelegateCliRun(
            state=EnumDelegateCliState.EXITED,
            exit_code=proc.returncode,
            stdout=proc.stdout,
        )
