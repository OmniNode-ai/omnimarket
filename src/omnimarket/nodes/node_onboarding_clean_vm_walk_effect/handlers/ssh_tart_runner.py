# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Execute Tart through a remote bash shell, including on zsh login hosts."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Sequence

from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    ModelRunnerResult,
)


def _remote_command(argv: Sequence[str]) -> str:
    executable = argv[0]
    if executable.startswith("~/"):
        executable = '"$HOME/"' + shlex.quote(executable[2:])
    else:
        executable = shlex.quote(executable)
    return " ".join((executable, *(shlex.quote(a) for a in argv[1:])))


class HandlerSshTart:
    """No local Tart installation is needed; every command runs on the SSH host."""

    def __init__(self, host: str) -> None:
        self._host = host

    def _ssh(self, command: str) -> list[str]:
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            self._host,
            "bash",
            "-c",
            shlex.quote(command),
        ]

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ModelRunnerResult:
        try:
            result = subprocess.run(
                self._ssh(_remote_command(argv)),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # Partial output may contain secrets; the guest log is read separately.
            return ModelRunnerResult(
                exit_code=124, stdout="", stderr="SSH command timed out"
            )
        return ModelRunnerResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def start(self, argv: Sequence[str]) -> None:
        if len(argv) != 4 or tuple(argv[1:3]) != ("run", "--no-graphics"):
            raise ValueError("only tart run --no-graphics may be detached")
        log = shlex.quote(f"/tmp/{argv[-1]}.tart.log")
        command = f"nohup {_remote_command(argv)} >{log} 2>&1 </dev/null &"
        result = subprocess.run(
            self._ssh(command),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise OSError(
                f"detached tart run failed ({result.returncode}): {result.stderr}"
            )
