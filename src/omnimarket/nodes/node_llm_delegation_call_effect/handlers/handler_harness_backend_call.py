# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run a delegation through the ``kind: harness`` backends the overlay declares (OMN-26219).

The routing overlay declares the harness backends, the tiers that serve a task class
and the order of its escalation chain. This handler resolves that chain with the
routing reducer's resolver, admits a rung only for the house tenant on the internal
surface (and for a pin-only rung only when the request pins that backend), runs one
attempt per admitted rung through the harness process port, judges every answer with
the delegation answer gate and climbs to the next rung when the gate rejects. Every
attempt, accepted or not, leaves one cost record. No model name, rung order, class
list or host is a literal here: all of them are read from the overlay.
"""

from __future__ import annotations

import logging

from omnimarket.enums.enum_harness_rung_refusal import EnumHarnessRungRefusal
from omnimarket.handlers.routing import routing_tiers_path as rtp
from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendKind,
    EnumDelegationBackendSurface,
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_harness_escalation_chain import (
    resolve_class_escalation_chain,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_harness_backend_call import (
    ModelHarnessAttemptRecord,
    ModelHarnessBackendCallRequest,
    ModelHarnessBackendCallResult,
    ModelHarnessRungRefusal,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.protocols.protocol_harness_backend_call import (
    ProtocolHarnessAnswerGate,
    ProtocolHarnessCostSink,
    ProtocolHarnessRunner,
)

logger = logging.getLogger(__name__)

_PINNED_TIER = "pinned"


def _refusals(
    backend: ModelDelegationBackendConfig,
    request: ModelHarnessBackendCallRequest,
) -> tuple[EnumHarnessRungRefusal, ...]:
    """Why ``request`` may not run on ``backend``; empty when it may.

    The executor repeats the routing check (OMN-20287): a non-house tenant and a
    non-internal surface are never admitted, and a pin-only backend only to a
    request that pins exactly that backend.
    """
    found: list[EnumHarnessRungRefusal] = []
    if backend.explicit_pin_only and request.pinned_backend_id != backend.backend_id:
        found.append(EnumHarnessRungRefusal.NOT_PIN_ROUTABLE)
    if request.tenant_slug != HOUSE_TENANT_SLUG:
        found.append(EnumHarnessRungRefusal.NOT_HOUSE_TENANT)
    if request.surface is not EnumDelegationBackendSurface.INTERNAL:
        found.append(EnumHarnessRungRefusal.NOT_INTERNAL_SURFACE)
    return tuple(found)


class HandlerHarnessBackendCall:
    """Walk the declared harness rungs of one task class: run, gate, record, escalate."""

    def __init__(
        self,
        *,
        runner: ProtocolHarnessRunner,
        gate: ProtocolHarnessAnswerGate,
        cost_sink: ProtocolHarnessCostSink,
        overlay: ModelDelegationRoutingOverlay | None = None,
    ) -> None:
        self._runner = runner
        self._gate = gate
        self._cost_sink = cost_sink
        self._overlay = overlay

    def _rungs(
        self,
        request: ModelHarnessBackendCallRequest,
        overlay: ModelDelegationRoutingOverlay,
    ) -> tuple[
        list[tuple[str, ModelDelegationBackendConfig]],
        list[ModelHarnessRungRefusal],
        str,
    ]:
        """The rungs to try in order, the rungs refused and a detail when none can run."""
        backends = overlay.backend_by_id()
        admitted: list[tuple[str, ModelDelegationBackendConfig]] = []
        refused: list[ModelHarnessRungRefusal] = []
        if request.pinned_backend_id is not None:
            pinned = backends.get(request.pinned_backend_id)
            if pinned is None:
                return (
                    [],
                    [],
                    f"pinned backend {request.pinned_backend_id!r} is not declared",
                )
            if request.task_class not in pinned.capabilities:
                return (
                    [],
                    [],
                    f"pinned backend {pinned.backend_id!r} does not declare task class {request.task_class!r}",
                )
            why = _refusals(pinned, request)
            if why:
                return (
                    [],
                    [
                        ModelHarnessRungRefusal(
                            tier=_PINNED_TIER,
                            backend_id=pinned.backend_id,
                            refusals=why,
                        )
                    ],
                    "the pinned backend refused the request",
                )
            return [(_PINNED_TIER, pinned)], [], ""
        chain = resolve_class_escalation_chain(
            request.task_class,
            tenant_id=request.tenant_slug,
            surface=request.surface,
            overlay=overlay,
        )
        if chain is None:
            return (
                [],
                [],
                f"the overlay declares no escalation chain for {request.task_class!r}",
            )
        for rung in chain.rungs:
            if rung.kind != "harness" or rung.backend_id is None:
                continue
            backend = backends[rung.backend_id]
            if backend.kind is not EnumDelegationBackendKind.HARNESS:
                continue
            why = _refusals(backend, request)
            if why:
                refused.append(
                    ModelHarnessRungRefusal(
                        tier=rung.tier, backend_id=backend.backend_id, refusals=why
                    )
                )
            else:
                admitted.append((rung.tier, backend))
        if not admitted:
            return [], refused, "no harness rung of the chain admits this request"
        return admitted, refused, ""

    async def handle(
        self, request: ModelHarnessBackendCallRequest
    ) -> ModelHarnessBackendCallResult:
        overlay = (
            self._overlay
            if self._overlay is not None
            else rtp.load_delegation_routing_overlay()
        )
        rungs, refused, detail = self._rungs(request, overlay)
        if not rungs:
            return ModelHarnessBackendCallResult(
                request_id=request.request_id,
                status="refused",
                ok=False,
                refused_rungs=tuple(refused),
                detail=detail,
            )
        attempts: list[ModelHarnessAttemptRecord] = []
        for number, (tier, backend) in enumerate(rungs, start=1):
            assert backend.harness is not None
            timeout = min(request.timeout_seconds, backend.timeout_ms / 1000)
            receipt = await self._runner.run(backend, request.prompt, timeout)
            verdict = (
                self._gate.judge(request, backend, receipt.answer)
                if receipt.exit_cause == "completed"
                else None
            )
            accepted = verdict is not None and verdict.accepted
            record = ModelHarnessAttemptRecord(
                request_id=request.request_id,
                attempt=number,
                tier=tier,
                backend_id=backend.backend_id,
                harness=backend.harness.value,
                model_name=receipt.model or backend.model_name,
                tokens_in=receipt.tokens_in,
                tokens_out=receipt.tokens_out,
                cost_usd=receipt.cost_usd,
                wall_seconds=receipt.wall_seconds,
                exit_cause=receipt.exit_cause,
                accepted=accepted,
                gate_kind=verdict.gate_kind if verdict is not None else None,
                gate_reasons=verdict.reasons if verdict is not None else (),
                caller_lane=request.caller_lane,
            )
            attempts.append(record)
            try:
                self._cost_sink.record(record)
            except Exception as exc:  # a cost record must never fail the call
                logger.warning(
                    "harness cost record for %s attempt %d not delivered: %s",
                    request.request_id,
                    number,
                    exc,
                )
            if accepted:
                return ModelHarnessBackendCallResult(
                    request_id=request.request_id,
                    status="completed",
                    ok=True,
                    content=receipt.answer,
                    backend_id=backend.backend_id,
                    model_name=receipt.model or backend.model_name,
                    attempts=tuple(attempts),
                    refused_rungs=tuple(refused),
                )
        return ModelHarnessBackendCallResult(
            request_id=request.request_id,
            status="rejected",
            ok=False,
            attempts=tuple(attempts),
            refused_rungs=tuple(refused),
            detail="every admitted harness rung was rejected by the gate or failed",
        )


__all__ = ["HandlerHarnessBackendCall"]
