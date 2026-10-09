# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports the policy gate run acts through (OMN-20671).

The handler takes its ports in its constructor, so the chains run against a scripted reader and
the deployment wires the local adapters. A read that fails is data, not an exception: it is a
``PolicyGateRead`` with ``api_ok=False`` that the compute node classifies and retries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PolicyGatePortError(RuntimeError):
    """A port could not be built (for example no GitHub token); the message names why."""


@dataclass(frozen=True)
class PolicyGateRead:
    """One read of a repo's latest completed handshake run, as the compute node takes it."""

    api_ok: bool
    api_error_text: str = ""
    total_count: int | None = None
    conclusion: str = ""


class ProtocolPolicyGateReader(Protocol):
    """Reads the two GitHub endpoints the gate needs, by the endpoint the compute node named."""

    def default_branch(self, endpoint: str) -> str:
        """The repo's default branch, or an empty string when the lookup failed."""
        ...

    def latest_run(self, endpoint: str) -> PolicyGateRead:
        """The newest completed run page of the handshake workflow on the branch."""
        ...


class ProtocolPolicyGateSleeper(Protocol):
    """Waits between two reads of one repo."""

    def sleep(self, seconds: int) -> None: ...
