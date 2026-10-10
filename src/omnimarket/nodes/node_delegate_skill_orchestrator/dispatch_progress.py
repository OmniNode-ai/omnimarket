# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep cancellation evidence scoped to one delegation, including child tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from omnibase_core.models.delegation.wire import EnumDelegationTerminalFailureCause

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegation_dispatch_progress import (
    DispatchStage,
    ModelDelegationDispatchProgress,
)

# Stages in which a provider call of the in-flight rung was running or had
# answered. Cancelled before them (the effect child still booting), no call
# left this host, so none is recorded.
_CALL_STAGES: frozenset[DispatchStage] = frozenset({"inference", "quality_gate"})

current_dispatch_progress: ContextVar[ModelDelegationDispatchProgress | None] = (
    ContextVar("delegation_dispatch_progress", default=None)
)


@contextmanager
def dispatch_stage(stage: DispatchStage) -> Iterator[None]:
    """Preserve the innermost cancelled or timed-out stage through cleanup."""
    progress = current_dispatch_progress.get()
    if progress is None:
        yield
        return
    previous_stage = progress.stage
    progress.stage = stage
    try:
        yield
    except asyncio.CancelledError:
        if progress.cancelled_stage is None:
            progress.cancelled_stage = progress.stage
        raise
    except TimeoutError:
        # A stage's own deadline can expire without cancelling this task.
        # Carry that stage through outer scopes and any subsequent cleanup.
        previous_stage = progress.stage
        raise
    finally:
        progress.stage = previous_stage


def budget_cancelled_result(
    progress: ModelDelegationDispatchProgress,
    *,
    cancel_message: str,
    cancelled_stage: DispatchStage,
) -> dict[str, object]:
    """The port-shaped failed result of a run the handler's budget cancelled.

    Pure: built only from what ``progress`` already holds, so the cancelled
    run's terminal carries the routing decision, the key provenance and every
    call made, in the same shape the port returns on its own failures. Each
    call the running effect reported as failed is a rung of its own (a 429
    before a re-aimed model), and the call the budget interrupted is the last
    rung, typed ``timeout`` with no accept/climb verdict, because none was
    reached. A port that recorded nothing yields a result naming nothing.
    """
    attempts = [dict(attempt) for attempt in progress.attempts]
    secret_source = progress.secret_source
    secret_ref = progress.secret_ref
    in_flight = progress.in_flight_attempt
    model_name = None if in_flight is None else in_flight.get("model_id")
    if in_flight is not None:
        interrupted: dict[str, object] | None = (
            dict(in_flight) if cancelled_stage in _CALL_STAGES else None
        )
        for call in progress.in_flight_calls:
            model_name = call.model_id
            if call.secret_source is not None:
                secret_source = call.secret_source
                secret_ref = progress.in_flight_secret_ref
            if call.phase == "started":
                interrupted = {**in_flight, "model_id": call.model_id}
            elif call.success:
                interrupted = {
                    **in_flight,
                    "model_id": call.model_id,
                    "http_status": call.http_status,
                }
            else:
                attempts.append(
                    {
                        **in_flight,
                        "model_id": call.model_id,
                        "quality_gate_passed": False,
                        "quality_score": None,
                        "cost_usd": 0.0,
                        "failure_class": (
                            None
                            if call.failure_class is None
                            else call.failure_class.value
                        ),
                        "http_status": call.http_status,
                        "error_message": call.error_message,
                        "acceptance_decision": (
                            EnumDelegationAcceptanceDecision.CLIMB.value
                        ),
                        "acceptance_reason": (
                            EnumDelegationAcceptanceReason.PROVIDER_CALL_FAILED.value
                        ),
                    }
                )
                interrupted = None
        if interrupted is not None:
            attempts.append(
                {
                    **interrupted,
                    "quality_gate_passed": False,
                    "quality_score": None,
                    "cost_usd": 0.0,
                    "failure_class": EnumDelegationFailureClass.TIMEOUT.value,
                    "error_message": cancel_message,
                }
            )
    elif attempts:
        model_name = attempts[-1].get("model_id")
    return {
        "status": "timeout",
        "content": "",
        "error_message": cancel_message,
        "terminal_failure_cause": EnumDelegationTerminalFailureCause.TIMEOUT.value,
        "delegated_to": progress.in_flight_endpoint_ref,
        "model_name": model_name,
        "secret_source": None if secret_source is None else secret_source.value,
        "secret_ref": secret_ref,
        "escalation_count": progress.escalation_count,
        "cost_usd": progress.cost_usd,
        "attempts": attempts,
        **({"attempts_count": len(attempts)} if attempts else {}),
    }
