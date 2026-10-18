# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two ports of the lab Claude token effect: the secret store and the host shell.

Production wires ``handlers.handler_local_token_store`` and
``handlers.handler_lab_host_shell``; tests wire a fake store and a fake host, so no
real token or host is ever touched by a test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from omnimarket.nodes.node_lab_claude_token_effect.models.model_lab_claude_token_request import (
    ModelLabTokenHost,
)


class ProtocolLabTokenStore(Protocol):
    """A secret store keyed by reference. Values are returned to the handler only."""

    def get(self, ref: str) -> str | None: ...

    def put(self, ref: str, value: str) -> None: ...

    def delete(self, ref: str) -> None: ...


@dataclass(frozen=True)
class ShellAnswer:
    returncode: int
    stdout: str


class ProtocolLabHostShell(Protocol):
    """Runs one shell script on a lab host. ``stdin`` carries a token when one is
    deployed, so a token is never part of an argv or a script."""

    def run(
        self, host: ModelLabTokenHost, script: str, *, stdin: str = "", timeout_s: float
    ) -> ShellAnswer: ...
