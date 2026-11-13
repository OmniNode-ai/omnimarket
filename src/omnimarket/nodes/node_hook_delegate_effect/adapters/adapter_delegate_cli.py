# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ProtocolDelegateCli over a real process: the `onex` on PATH, run with a time limit."""

from __future__ import annotations

import shutil
import subprocess

from omnimarket.nodes.node_hook_delegate_effect.models import (
    EnumDelegateCliState,
    ModelDelegateCliRun,
)


class AdapterDelegateCli:
    """Runs `onex` from PATH; a missing `onex` or a start failure is reported, not raised."""

    def run(self, args: list[str], timeout_seconds: float) -> ModelDelegateCliRun:
        onex = shutil.which("onex")
        if not onex:
            return ModelDelegateCliRun(state=EnumDelegateCliState.NOT_FOUND)
        try:
            proc = subprocess.run(
                [onex, *args],
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
