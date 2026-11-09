# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parity check as a dependency: a protocol, the real subprocess and an in-memory fake."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ModelRunOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int
    stdout: str = ""
    stderr: str = ""


class ProtocolParityCheck(Protocol):
    def run(
        self,
        command: Sequence[str],
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> ModelRunOutcome:
        """Run the parity check; raise OSError or TimeoutError when it did not run."""


class SubprocessParityCheck:
    """Runs the command with the extra variables added to this process's environment."""

    def run(
        self,
        command: Sequence[str],
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> ModelRunOutcome:
        import os

        try:
            done = subprocess.run(
                list(command),
                env={**os.environ, **environment},
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(str(exc)) from exc
        return ModelRunOutcome(
            returncode=done.returncode, stdout=done.stdout, stderr=done.stderr
        )


class InMemoryParityCheck:
    """Answers every call with one canned outcome and records the commands and variables it was given."""

    def __init__(
        self, outcome: ModelRunOutcome | None = None, fail: str | None = None
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self._outcome = (
            outcome if outcome is not None else ModelRunOutcome(returncode=0)
        )
        self._fail = fail

    def run(
        self,
        command: Sequence[str],
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> ModelRunOutcome:
        self.calls.append((list(command), dict(environment)))
        if self._fail is not None:
            raise OSError(self._fail)
        return self._outcome
