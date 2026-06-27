# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Redeploy ORCHESTRATOR handler (OMN-13211 / B3).

Re-expresses the ``node_redeploy`` ``HandlerRedeployWorkflowRunner`` as a
canonical ORCHESTRATOR. It owns phase sequencing but dispatches commands OVER THE
BUS — it never constructs sibling handlers in-process, never runs an in-process
FSM loop, and never does I/O. ``handle(envelope)`` returns
``ModelHandlerOutput.for_orchestrator`` carrying the next command(s) as event
envelopes the runtime publishes; the orchestrator reacts to the resulting gate /
deploy / readiness events.

Flow (event-driven, no in-process loop):

  1. consume ``redeploy-start`` -> emit the prod-promotion-gate-evaluate command
     (-> node_prod_promotion_gate_compute). The gate runs BEFORE any deploy
     effect, so a bad prod request is rejected before the agent is invoked.
  2. consume ``prod-promotion-gate-evaluated``:
       - allowed   -> emit the deploy-publish command (-> node_redeploy_deploy_effect),
                      threading the gate's resolved digest + rollback target.
       - blocked   -> emit the redeploy-completed event with final_phase=BLOCKED.
  3. the deploy effect publishes the deploy-agent command + emits the result /
     rolled-back fact; the orchestrator consumes those completion events to emit
     the readiness-gate-start command and, finally, redeploy-completed.

Non-prod lanes pass the gate trivially (the compute node allows them), so the
same dispatch path serves dev / stability-test / prod with no branch in the
orchestrator.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumGrantResolution,
    EnumRedeployPhase,
    EnumRuntimeLane,
    ModelDeployPublishCommand,
    ModelProdPromotionGateCommand,
    ModelProdPromotionGateDecision,
    ModelProdPromotionGrant,
    ModelProdPromotionGrantResolveCommand,
    ModelProdPromotionGrantResolvedEvent,
    ModelRedeployCommand,
    ModelRedeployCompletedEvent,
    ModelRuntimeImageBuilt,
)
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_redeploy_orchestrator.models.model_redeploy_start_command import (
    ModelRedeployStartCommand,
)

HANDLER_ID = "redeploy-orchestrator"

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_PUBLISH = contract_publish_topics(_CONTRACT)


def _topic_with_suffix(suffix: str) -> str:
    """Resolve exactly one contract publish topic ending with ``suffix``."""
    matches = [t for t in _PUBLISH if t.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(
            f"Contract {_CONTRACT} must declare exactly one event_bus.publish_topics "
            f"topic ending in {suffix!r}; found {matches}"
        )
    return matches[0]


TOPIC_GRANT_RESOLVE = _topic_with_suffix("prod-promotion-grant-resolve.v1")
TOPIC_PROD_GATE_EVALUATE = _topic_with_suffix("prod-promotion-gate-evaluate.v1")
TOPIC_DEPLOY_PUBLISH = _topic_with_suffix("redeploy-deploy-publish.v1")
TOPIC_REDEPLOY_COMPLETED = _topic_with_suffix("redeploy-completed.v1")


class HandlerRedeployOrchestrator:
    """Canonical orchestrator: dispatch redeploy commands over the bus."""

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Route a redeploy lifecycle event to the next bus command.

        ``redeploy-start`` (prod) -> prod-promotion-grant-resolve command;
        ``redeploy-start`` (non-prod) -> prod-gate-evaluate command directly.
        ``prod-promotion-grant-resolved`` -> prod-gate-evaluate command with the
        out-of-band resolved grant + evaluated_at stamped (NEVER from the start).
        ``prod-promotion-gate-evaluated`` -> deploy-publish command (allowed) or
        redeploy-completed:BLOCKED (denied).
        """
        event_type = envelope.event_type or ""
        correlation_id = envelope.correlation_id or uuid4()

        if event_type.endswith("prod-promotion-grant-resolved.v1"):
            events = self._on_grant_resolved(envelope, correlation_id)
        elif event_type.endswith("prod-promotion-gate-evaluated.v1"):
            events = self._on_gate_evaluated(envelope, correlation_id)
        elif event_type.endswith("runtime-image-built.v1"):
            # OMN-13655: cloud CI build-complete event — coerce into a start command
            # and route through the prod-promotion gate. This is the canonical path
            # that replaces the imperative git/gh/docker-driven cloud-redeploy skill.
            events = self._on_image_built(envelope, correlation_id)
        else:
            # Default entrypoint: the redeploy-start command.
            events = self._on_start(envelope, correlation_id)

        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=envelope.envelope_id,
            correlation_id=correlation_id,
            handler_id=HANDLER_ID,
            events=tuple(events),
        )

    def _on_start(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        """Route a redeploy-start request to the next command.

        Prod requests MUST resolve the promotion grant out-of-band first
        (OMN-13439 Phase-2b resolver EFFECT reads the grant from
        ``onex_change_control@main``), so prod emits the grant-resolve command and
        only reaches the gate after the resolved fact rides back. Non-prod lanes
        need no grant — the gate trivially allows them — so they go straight to the
        gate-evaluate command, leaving dev/stability dispatch unchanged.
        """
        start = _coerce_start(envelope.payload, correlation_id)
        if start.runtime_lane is EnumRuntimeLane.PROD:
            return self._emit_grant_resolve(start)
        return self._emit_gate_evaluate(start, grant=None, evaluated_at=None)

    def _emit_grant_resolve(
        self, start: ModelRedeployStartCommand
    ) -> list[ModelEventEnvelope[Any]]:
        """Emit the grant-resolve command for a prod redeploy-start request.

        The deterministic ``evaluated_at`` is stamped at this orchestration
        boundary and threaded through the resolver back into the gate command, so
        the COMPUTE gate never reads a clock. The caller-supplied
        ``start.promotion_grant`` is DROPPED — a request cannot author the
        authorization that approves it; the resolver reads it from the durable
        anchor on ``@main``.
        """
        resolve_command = ModelProdPromotionGrantResolveCommand(
            correlation_id=start.correlation_id,
            runtime_lane=start.runtime_lane,
            requested_image_digest=start.image_digest,
            promotion_batch_id=start.promotion_batch_id,
            requested_by=start.requested_by,
            evaluated_at=datetime.now(UTC),
        )
        return [
            ModelEventEnvelope(
                payload=resolve_command,
                correlation_id=start.correlation_id,
                event_type=TOPIC_GRANT_RESOLVE,
            )
        ]

    def _emit_gate_evaluate(
        self,
        start: ModelRedeployStartCommand,
        *,
        grant: ModelProdPromotionGrant | None,
        evaluated_at: datetime | None,
    ) -> list[ModelEventEnvelope[Any]]:
        """Emit the prod-gate-evaluate command.

        ``grant`` + ``evaluated_at`` come ONLY from the out-of-band resolver
        (Phase-2b); they are NEVER taken from ``start.promotion_grant``. For
        non-prod lanes both are ``None`` and the gate allows trivially.
        ``requested_by`` is threaded so the gate enforces
        ``approved_by != requested_by``.
        """
        gate_command = ModelProdPromotionGateCommand(
            correlation_id=start.correlation_id,
            runtime_lane=start.runtime_lane,
            requested_image_digest=start.image_digest,
            promotion_batch_id=start.promotion_batch_id,
            readiness_projection=start.readiness_projection,
            occ_gate_state=start.occ_gate_state,
            rollback_target=start.rollback_target,
            previous_image=start.previous_image,
            requested_by=start.requested_by,
            promotion_grant=grant,
            evaluated_at=evaluated_at,
        )
        return [
            ModelEventEnvelope(
                payload=gate_command,
                correlation_id=start.correlation_id,
                event_type=TOPIC_PROD_GATE_EVALUATE,
            )
        ]

    def _on_grant_resolved(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        """Thread the out-of-band resolved grant into the gate-evaluate command.

        The resolver EFFECT emits ``ModelProdPromotionGrantResolvedEvent`` plus the
        echoed original start request. The orchestrator stamps the RESOLVED grant
        (``None`` for every non-RESOLVED outcome, so the gate fails closed) and the
        resolver's deterministic ``evaluated_at`` onto the gate command. The grant
        is NEVER taken from ``start.promotion_grant``.
        """
        resolved, start = _coerce_grant_resolved(envelope.payload, correlation_id)
        grant = (
            resolved.grant
            if resolved.resolution is EnumGrantResolution.RESOLVED
            else None
        )
        return self._emit_gate_evaluate(
            start, grant=grant, evaluated_at=resolved.evaluated_at
        )

    def _on_gate_evaluated(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        """React to the gate decision: deploy if allowed, else complete BLOCKED."""
        decision, start, gate_command = _coerce_gate_result(
            envelope.payload, correlation_id
        )

        if not decision.allowed:
            completed = ModelRedeployCompletedEvent(
                correlation_id=correlation_id,
                final_phase=EnumRedeployPhase.BLOCKED,
                phases_completed=0,
                error_message=decision.reason,
            )
            return [
                ModelEventEnvelope(
                    payload=completed,
                    correlation_id=correlation_id,
                    event_type=TOPIC_REDEPLOY_COMPLETED,
                )
            ]

        # OMN-13440: thread the verified grant + batch + evaluated_at from the gate
        # command into the deploy-publish command so the deploy EFFECT can re-verify
        # target binding at its own boundary (defense-in-depth). The grant is taken
        # ONLY from the gate command (resolved out-of-band by Phase-2b), NEVER from
        # the start request. For a prod deploy with no gate command (gate command not
        # echoed), promotion_grant stays None and the deploy EFFECT fails closed.
        promotion_grant = gate_command.promotion_grant if gate_command else None
        evaluated_at = gate_command.evaluated_at if gate_command else None
        promotion_batch_id = (
            gate_command.promotion_batch_id
            if gate_command
            else start.promotion_batch_id
        )

        publish_command = ModelDeployPublishCommand(
            correlation_id=correlation_id,
            scope=start.scope,
            git_ref=start.git_ref,
            runtime_lane=start.runtime_lane,
            build_source=start.build_source,
            services=start.services,
            image_ref=start.image_ref,
            image_digest=decision.image_digest or start.image_digest,
            promotion_batch_id=promotion_batch_id,
            promotion_grant=promotion_grant,
            evaluated_at=evaluated_at,
            requested_by=start.requested_by,
            smoke_test=start.smoke_test,
            rollback_target=(
                decision.rollback_target
                or start.rollback_target
                or start.previous_image
            ),
        )
        return [
            ModelEventEnvelope(
                payload=publish_command,
                correlation_id=correlation_id,
                event_type=TOPIC_DEPLOY_PUBLISH,
            )
        ]

    def _on_image_built(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        """Route a cloud CI build-complete event to the next command (OMN-13655).

        The ``runtime-image-built.v1`` event is the contract-sourced entrypoint
        for the canonical cloud redeploy path. It replaces the imperative
        git/gh/docker-driven skill. The orchestrator coerces it into a
        ``ModelRedeployStartCommand`` and routes identically to ``_on_start`` so
        the prod-promotion gate, deploy-publish EFFECT, and FSM reducer all run
        on the same proven bus path regardless of the entrypoint event type.

        Prod-lane builds route through the grant-resolver EFFECT first; non-prod
        builds proceed straight to the gate (which trivially allows them).
        """
        built = _coerce_image_built(envelope.payload, correlation_id)
        start = ModelRedeployStartCommand(
            correlation_id=built.correlation_id,
            runtime_lane=built.runtime_lane,
            image_digest=built.digest,
            image_ref=built.image_ref,
            promotion_batch_id=built.promotion_batch_id,
            build_source=built.build_source,
            # promotion_class and non_main_lineage are inferred from build_source;
            # the gate reads promotion_class from the gate command (OMN-13656).
            requested_by="node_redeploy_orchestrator[image-built]",
        )
        if start.runtime_lane is EnumRuntimeLane.PROD:
            return self._emit_grant_resolve(start)
        return self._emit_gate_evaluate(start, grant=None, evaluated_at=None)


def _coerce_start(payload: Any, correlation_id: UUID) -> ModelRedeployStartCommand:
    """Coerce the start payload into a ``ModelRedeployStartCommand``."""
    if isinstance(payload, ModelRedeployStartCommand):
        return payload
    if isinstance(payload, ModelRedeployCommand):
        return ModelRedeployStartCommand(
            correlation_id=payload.correlation_id,
            scope="full",
            git_ref="origin/main",
            runtime_lane=payload.runtime_lane,
            image_ref=payload.image_ref,
            image_digest=payload.image_digest,
            promotion_batch_id=payload.promotion_batch_id,
        )
    if isinstance(payload, Mapping):
        data = dict(payload)
        data.setdefault("correlation_id", str(correlation_id))
        return ModelRedeployStartCommand.model_validate(data)
    if hasattr(payload, "model_dump"):
        return ModelRedeployStartCommand.model_validate(payload.model_dump())
    raise TypeError(
        f"redeploy-start payload must be ModelRedeployStartCommand or a mapping; "
        f"got {type(payload).__name__}"
    )


def _coerce_gate_result(
    payload: Any, correlation_id: UUID
) -> tuple[
    ModelProdPromotionGateDecision,
    ModelRedeployStartCommand,
    ModelProdPromotionGateCommand | None,
]:
    """Coerce the gate-evaluated payload into (decision, start, gate command).

    The runtime delivers the gate-evaluated event whose payload carries the
    ``ModelProdPromotionGateDecision`` (under ``decision``), the echoed original
    start request (under ``start``), and — for prod — the echoed gate command
    (under ``command``) that carries the out-of-band-resolved promotion grant +
    ``evaluated_at``. The orchestrator threads that verified grant into the deploy
    command so the deploy EFFECT can re-verify target binding (OMN-13440). The gate
    command is ``None`` when not echoed (non-prod, or a digest-only deploy).
    """
    mapping = _as_mapping(payload)
    if mapping is None:
        raise TypeError(
            f"gate-evaluated payload must be a mapping or model; got "
            f"{type(payload).__name__}"
        )

    decision_raw = mapping.get("decision", mapping)
    decision = ModelProdPromotionGateDecision.model_validate(_as_dict(decision_raw))

    start_raw = mapping.get("start")
    if start_raw is not None:
        start = ModelRedeployStartCommand.model_validate(_as_dict(start_raw))
    else:
        # Minimal start when only the decision rode through (digest-only deploy).
        start = ModelRedeployStartCommand(
            correlation_id=correlation_id,
            image_digest=decision.image_digest,
            rollback_target=decision.rollback_target,
        )

    command_raw = mapping.get("command")
    gate_command = (
        ModelProdPromotionGateCommand.model_validate(_as_dict(command_raw))
        if command_raw is not None
        else None
    )
    return decision, start, gate_command


def _coerce_grant_resolved(
    payload: Any, correlation_id: UUID
) -> tuple[ModelProdPromotionGrantResolvedEvent, ModelRedeployStartCommand]:
    """Coerce the grant-resolved payload into (resolved event, original start).

    The runtime delivers the resolved event whose payload carries the
    ``ModelProdPromotionGrantResolvedEvent`` (under a ``resolved`` key) and the
    echoed original start request (under a ``start`` key) so the orchestrator can
    build the gate command without rehydrating state. A bare resolved event (no
    echoed start) is rebuilt into a minimal prod start from the resolved fact so
    the gate still runs against the resolved grant.
    """
    mapping = _as_mapping(payload)
    if mapping is None:
        raise TypeError(
            f"grant-resolved payload must be a mapping or model; got "
            f"{type(payload).__name__}"
        )

    resolved_raw = mapping.get("resolved", mapping)
    resolved = ModelProdPromotionGrantResolvedEvent.model_validate(
        _as_dict(resolved_raw)
    )

    start_raw = mapping.get("start")
    if start_raw is not None:
        start = ModelRedeployStartCommand.model_validate(_as_dict(start_raw))
    else:
        grant = resolved.grant
        start = ModelRedeployStartCommand(
            correlation_id=resolved.correlation_id or correlation_id,
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest=grant.approved_image_digest if grant is not None else None,
            promotion_batch_id=(
                grant.approved_promotion_batch_id if grant is not None else None
            ),
        )
    return resolved, start


def _coerce_image_built(payload: Any, correlation_id: UUID) -> ModelRuntimeImageBuilt:
    """Coerce the runtime-image-built payload into a ``ModelRuntimeImageBuilt``.

    The event may arrive as a model instance, a mapping, or any object with a
    ``model_dump`` method. Falls closed with ``TypeError`` on unknown shapes.
    """
    if isinstance(payload, ModelRuntimeImageBuilt):
        return payload
    if isinstance(payload, Mapping):
        data = dict(payload)
        data.setdefault("correlation_id", str(correlation_id))
        return ModelRuntimeImageBuilt.model_validate(data)
    if hasattr(payload, "model_dump"):
        return ModelRuntimeImageBuilt.model_validate(payload.model_dump())
    raise TypeError(
        f"runtime-image-built payload must be ModelRuntimeImageBuilt or a mapping; "
        f"got {type(payload).__name__}"
    )


def _as_mapping(candidate: Any) -> Mapping[str, Any] | None:
    if isinstance(candidate, Mapping):
        return candidate
    model_dump = getattr(candidate, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return dumped
    return None


def _as_dict(candidate: Any) -> dict[str, Any]:
    mapping = _as_mapping(candidate)
    if mapping is None:
        raise TypeError(f"expected a mapping/model; got {type(candidate).__name__}")
    return dict(mapping)


__all__: list[str] = [
    "HANDLER_ID",
    "TOPIC_DEPLOY_PUBLISH",
    "TOPIC_GRANT_RESOLVE",
    "TOPIC_PROD_GATE_EVALUATE",
    "TOPIC_REDEPLOY_COMPLETED",
    "HandlerRedeployOrchestrator",
]
