# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure deterministic delegation escalation/tier decision — no I/O, no side effects.

OMN-13476 (epic OMN-13471): the escalation decision — *whether* to escalate and
*to which tier* — was inlined twice in
``node_delegation_orchestrator.handlers.handler_delegation_workflow`` (the
inference-error branch and the quality-gate-fail branch). Both branches applied
the same precedence rules. This COMPUTE owns that single decision; the
orchestrator now resolves the config-dependent inputs (the next eligible tier
and the no-higher-tier reason, which read the routing contract+overlay) and
delegates the verdict here.

The decision precedence, preserved verbatim from the orchestrator branches:

1. failure is non-retryable  -> terminate (``non_retryable_reason``)
2. escalation budget reached  -> terminate ("max_escalation_attempts_reached")
3. current tier unknown        -> terminate ("current_tier_unknown")
4. no higher tier available    -> terminate (``no_higher_tier_reason``)
5. otherwise                   -> escalate to ``next_tier_name``

This is a NodeCompute archetype: stateless, deterministic, zero I/O. Tiers,
providers and models are resolved upstream from the routing contract+overlay and
arrive as plain values — this handler never reads env, files, the bus, or a DB.
"""

from __future__ import annotations

from omnimarket.routing.model_escalation_decision_request import (
    ModelEscalationDecisionRequest,
)
from omnimarket.routing.model_escalation_decision_result import (
    ModelEscalationDecisionResult,
)

_MAX_ATTEMPTS_REACHED_REASON = "max_escalation_attempts_reached"
_CURRENT_TIER_UNKNOWN_REASON = "current_tier_unknown"


def _terminal(reason: str) -> ModelEscalationDecisionResult:
    return ModelEscalationDecisionResult(
        can_escalate=False,
        next_tier_name=None,
        terminal_failure_reason=reason,
    )


class HandlerEscalationDecision:
    """Decide whether a failed delegation attempt escalates and to which tier."""

    def handle(
        self, request: ModelEscalationDecisionRequest
    ) -> ModelEscalationDecisionResult:
        # 1. Non-retryable failure: terminate with the supplied reason.
        if not request.error_retryable:
            return _terminal(request.non_retryable_reason)

        # 2. Escalation budget exhausted.
        if request.escalation_count >= request.max_escalation_attempts:
            return _terminal(_MAX_ATTEMPTS_REACHED_REASON)

        # 3. Current tier could not be identified.
        if request.current_tier_name is None:
            return _terminal(_CURRENT_TIER_UNKNOWN_REASON)

        # 4. Ladder exhausted — no routable higher tier. The orchestrator resolves
        #    the precise reason from the routing contract; require it here so the
        #    terminal record is never a bare/empty string.
        if request.next_tier_name is None:
            reason = request.no_higher_tier_reason
            if reason is None:
                raise ValueError(
                    "next_tier_name is None but no_higher_tier_reason was not "
                    "supplied; the orchestrator must resolve the precise "
                    "no-higher-tier reason before delegating the decision."
                )
            return _terminal(reason)

        # 5. Escalate to the resolved next tier.
        return ModelEscalationDecisionResult(
            can_escalate=True,
            next_tier_name=request.next_tier_name,
            terminal_failure_reason=None,
        )


__all__ = ["HandlerEscalationDecision"]
