# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports the merge-sweep lane handler acts through (OMN-20676).

The handler takes its ports in its constructor, so the chains run against fakes or the real
runner and the deployment wires the local adapters. A port that cannot run raises
``MergeSweepPortError``; the handler turns it into a typed result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class MergeSweepPortError(RuntimeError):
    """A port could not run; the message names what failed."""


@dataclass(frozen=True)
class MergeSweepCommandOutcome:
    """The exit status and output of one runner call."""

    returncode: int
    stdout: str
    stderr: str


class ProtocolMergeSweepLaneLauncher(Protocol):
    """Runs ``python3 <runner script> <args>`` with no shell."""

    def run(
        self, argv: Sequence[str], timeout_s: float
    ) -> MergeSweepCommandOutcome: ...


class ProtocolMergeSweepLaneFiles(Protocol):
    """Writes a lane's brief and reads its receipt file."""

    def write_text(self, path: str, text: str) -> None: ...

    def read_json(self, path: str) -> dict[str, Any] | None: ...
