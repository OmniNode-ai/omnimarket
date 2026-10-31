# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Running a read-only host command, behind a protocol the tests replace."""

import subprocess
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.node_timeouts_reader import load_node_timeouts


class ModelHostCommandOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int
    stdout: str
    stderr: str = ""


class ProtocolHostCommandRunner(Protocol):
    def run(self, argv: Sequence[str]) -> ModelHostCommandOutcome: ...


class SubprocessHostCommand:
    """Runs an argv with no shell; raises OSError when the binary is missing."""

    def __init__(self, timeout_seconds: float | None = None) -> None:
        self._timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else load_node_timeouts("node_automation_run_observer_effect").get(
                "host_command_timeout_seconds"
            )
        )

    def run(self, argv: Sequence[str]) -> ModelHostCommandOutcome:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )
        return ModelHostCommandOutcome(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
