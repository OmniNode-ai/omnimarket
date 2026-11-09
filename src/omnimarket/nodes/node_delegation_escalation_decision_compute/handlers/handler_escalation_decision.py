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
2. backend pinned by caller   -> terminate ("pinned_backend_failed", OMN-20844)
3. escalation budget reached  -> terminate ("max_escalation_attempts_reached")
4. current tier unknown        -> terminate ("current_tier_unknown")
5. no higher tier available    -> terminate (``no_higher_tier_reason``)
6. otherwise                   -> escalate to ``next_tier_name``

OMN-21213: each task class declares ``retry_on``, ``move_on``, ``skip_on`` and
``terminate_on`` in ``task_class_contracts.v1.yaml``. ``decide_transition`` reads
a classified cause's transition from those lists, and ``handle`` does the same
whenever the caller passes the class's lists and the cause. Both delegation
paths call this node for their move decisions: the bus orchestrator and the
in-process local port.

This is a NodeCompute archetype: stateless, deterministic, zero I/O. Tiers,
providers and models are resolved upstream from the routing contract+overlay and
arrive as plain values — this handler never reads env, files, the bus, or a DB.
"""

from __future__ import annotations

from omnimarket.models.delegation.model_escalation_transitions import (
    EnumEscalationCause,
    EnumMoveKind,
    EnumTransitionKind,
)
from omnimarket.models.delegation.model_paid_rung_decision import (
    ModelPaidRungDecision,
    ModelPaidRungRequest,
)
from omnimarket.models.delegation.model_transition_decision import (
    ModelTransitionDecision,
    ModelTransitionDecisionRequest,
)
from omnimarket.models.routing.model_escalation_decision_request import (
    ModelEscalationDecisionRequest,
)
from omnimarket.models.routing.model_escalation_decision_result import (
    ModelEscalationDecisionResult,
)

_MAX_ATTEMPTS_REACHED_REASON = "max_escalation_attempts_reached"
_CURRENT_TIER_UNKNOWN_REASON = "current_tier_unknown"
PINNED_BACKEND_FAILED_REASON = "pinned_backend_failed"


def _terminal(reason: str) -> ModelEscalationDecisionResult:
    return ModelEscalationDecisionResult(
        can_escalate=False,
        next_tier_name=None,
        terminal_failure_reason=reason,
    )


class HandlerEscalationDecision:
    """Decide whether a failed delegation attempt escalates and to which tier."""

    def decide_transition(
        self, request: ModelTransitionDecisionRequest
    ) -> ModelTransitionDecision:
        """Read ``request.cause``'s transition from the class's declared lists.

        A retry_on cause past the rung's retry budget becomes
        ``retry_budget_spent``. A move_on cause on a pinned run becomes
        ``pinned_backend_failed``; a tier climb at the escalation limit becomes
        ``escalation_limit``; a move with no next rung becomes
        ``ladder_exhausted``. Each of those is then read from the lists too.
        """
        table = request.transitions
        cause = request.cause
        kind = table.kind_of(cause)
        if kind is EnumTransitionKind.RETRY:
            if request.retries_on_rung < request.rung_retry_budget:
                return ModelTransitionDecision(kind=kind, cause=cause)
            cause = EnumEscalationCause.RETRY_BUDGET_SPENT
            kind = table.kind_of(cause)
        if kind is not EnumTransitionKind.MOVE:
            return ModelTransitionDecision(kind=kind, cause=cause)
        if request.pinned:
            cause = EnumEscalationCause.PINNED_BACKEND_FAILED
        elif (
            request.move_kind is EnumMoveKind.TIER_CLIMB
            and request.max_escalations is not None
            and request.escalation_count >= request.max_escalations
        ):
            cause = EnumEscalationCause.ESCALATION_LIMIT
        elif request.next_rung_available is False:
            cause = EnumEscalationCause.LADDER_EXHAUSTED
        else:
            return ModelTransitionDecision(
                kind=kind, cause=cause, move_kind=request.move_kind
            )
        return ModelTransitionDecision(kind=table.kind_of(cause), cause=cause)

    def decide_paid_rung(self, request: ModelPaidRungRequest) -> ModelPaidRungDecision:
        """OMN-23180: a paid rung below the contract's size band is a declared skip.

        A free rung, or a contract that declares no ``paid_from_size_band``, is
        allowed. A request whose band was not measured is below the band: only a
        measured large task reaches a paid rung. The skip is read from the
        class's declared lists, like every other cause.
        """
        if not request.rung_is_paid or request.paid_from_size_band is None:
            return ModelPaidRungDecision(allowed=True)
        measured = request.size_band
        if measured is not None and measured.rank >= request.paid_from_size_band.rank:
            return ModelPaidRungDecision(allowed=True)
        cause = EnumEscalationCause.SIZE_BAND_BELOW_PAID
        kind = request.transitions.kind_of(cause)
        band = measured.value if measured is not None else "unmeasured"
        return ModelPaidRungDecision(
            allowed=False,
            transition=ModelTransitionDecision(kind=kind, cause=cause),
            reason=(
                f"paid rung skipped before dispatch: the request's size band is "
                f"{band}, below {request.paid_from_size_band.value}, the smallest "
                "band that may reach a paid rung"
            ),
        )

    def handle(
        self, request: ModelEscalationDecisionRequest
    ) -> ModelEscalationDecisionResult:
        if request.cause is not None and request.transitions is not None:
            return self._handle_declared(request)
        # 1. Non-retryable failure: terminate with the supplied reason.
        if not request.error_retryable:
            return _terminal(request.non_retryable_reason)

        # 2. OMN-20844: a caller-pinned run runs on the pin or fails. Any
        #    escalation would answer it from another backend under the pin.
        if request.pinned_backend_ref is not None:
            return _terminal(PINNED_BACKEND_FAILED_REASON)

        # 3. Escalation budget exhausted.
        if request.escalation_count >= request.max_escalation_attempts:
            return _terminal(_MAX_ATTEMPTS_REACHED_REASON)

        # 4. Current tier could not be identified.
        if request.current_tier_name is None:
            return _terminal(_CURRENT_TIER_UNKNOWN_REASON)

        # 5. Ladder exhausted — no routable higher tier. The orchestrator resolves
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

        # 6. Escalate to the resolved next tier.
        return ModelEscalationDecisionResult(
            can_escalate=True,
            next_tier_name=request.next_tier_name,
            terminal_failure_reason=None,
        )

    def _handle_declared(
        self, request: ModelEscalationDecisionRequest
    ) -> ModelEscalationDecisionResult:
        """``handle`` for a caller that passed the class's lists and the cause.

        The terminal reasons are the ones the precedence above records, so a
        receipt reads the same whichever form the caller used.
        """
        assert request.cause is not None
        assert request.transitions is not None
        decision = self.decide_transition(
            ModelTransitionDecisionRequest(
                cause=request.cause,
                transitions=request.transitions,
                pinned=request.pinned_backend_ref is not None,
                escalation_count=request.escalation_count,
                max_escalations=request.max_escalation_attempts,
                next_rung_available=(
                    request.current_tier_name is not None
                    and request.next_tier_name is not None
                ),
            )
        )
        if decision.kind is EnumTransitionKind.MOVE:
            return ModelEscalationDecisionResult(
                can_escalate=True,
                next_tier_name=request.next_tier_name,
                terminal_failure_reason=None,
            )
        if decision.kind is not EnumTransitionKind.TERMINATE:
            raise ValueError(
                f"cause {request.cause.value!r} is declared {decision.kind.value}; "
                "an escalate-or-terminate caller must handle it before asking"
            )
        if decision.cause is EnumEscalationCause.PINNED_BACKEND_FAILED:
            return _terminal(PINNED_BACKEND_FAILED_REASON)
        if decision.cause is EnumEscalationCause.ESCALATION_LIMIT:
            return _terminal(_MAX_ATTEMPTS_REACHED_REASON)
        if decision.cause is EnumEscalationCause.LADDER_EXHAUSTED:
            if request.current_tier_name is None:
                return _terminal(_CURRENT_TIER_UNKNOWN_REASON)
            if request.no_higher_tier_reason is None:
                raise ValueError(
                    "next_tier_name is None but no_higher_tier_reason was not "
                    "supplied; the orchestrator must resolve the precise "
                    "no-higher-tier reason before delegating the decision."
                )
            return _terminal(request.no_higher_tier_reason)
        return _terminal(request.non_retryable_reason)


__all__ = ["PINNED_BACKEND_FAILED_REASON", "HandlerEscalationDecision"]
