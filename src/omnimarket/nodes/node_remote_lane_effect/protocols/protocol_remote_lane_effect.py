# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports the remote-lane effect handlers act through (OMN-20669).

Every handler takes its ports in its constructor, so the chains run against fakes
or real git and the deployment wires the local adapters. A port that cannot run
raises ``RemoteLanePortError``; the handler turns it into a typed result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class RemoteLanePortError(RuntimeError):
    """A port could not run; the message names what failed."""


@dataclass(frozen=True)
class RemoteLaneCommandOutcome:
    """The exit status and output of one command a port ran."""

    returncode: int
    stdout: str
    stderr: str


class ProtocolRemoteLaneGit(Protocol):
    """Reads a repository's refs: whether a canonical clone exists, and ``git`` itself."""

    def is_clone(self, path: str) -> bool: ...

    def run(
        self, argv: Sequence[str], timeout_s: float
    ) -> RemoteLaneCommandOutcome: ...
