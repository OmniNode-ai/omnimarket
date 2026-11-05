# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The idle-turn guard decision handler (OMN-19998): the shared rule, with the contract's policy injected."""

from __future__ import annotations

from omnimarket.handlers.rules_idle_turn_guard import (
    decide_idle_turn,
    load_idle_turn_guard_policy,
)
from omnimarket.models.idle_turn_guard import (
    ModelIdleTurnGuardDecision,
    ModelIdleTurnGuardDecisionRequest,
    ModelIdleTurnGuardPolicy,
)


class HandlerIdleTurnGuardDecision:
    def __init__(self, policy: ModelIdleTurnGuardPolicy | None = None) -> None:
        self._policy = policy or load_idle_turn_guard_policy()

    def handle(
        self, request: ModelIdleTurnGuardDecisionRequest
    ) -> ModelIdleTurnGuardDecision:
        return decide_idle_turn(request, self._policy)


__all__ = ["HandlerIdleTurnGuardDecision"]
