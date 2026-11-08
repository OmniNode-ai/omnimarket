# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_landing_canary_effect: the test repository the canary opens its pull request in."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    ModelCanaryOpened,
    ModelCanaryOpenRequest,
    ModelCanaryState,
)


class CanaryRepoError(RuntimeError):
    """The test repository could not be written or read; the message is a typed reason."""


class ProtocolCanaryRepo(Protocol):
    def open(self, request: ModelCanaryOpenRequest) -> ModelCanaryOpened:
        """Open one test pull request; raises ``CanaryRepoError`` with a typed reason."""
        ...

    def state(self, number: int) -> ModelCanaryState:
        """The pull request's state; raises ``CanaryRepoError`` when it cannot be read."""
        ...


__all__ = ["CanaryRepoError", "ProtocolCanaryRepo"]
