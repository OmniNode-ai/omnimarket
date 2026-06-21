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
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumRedeployPhase,
    ModelDeployPublishCommand,
    ModelProdPromotionGateCommand,
    ModelProdPromotionGateDecision,
    ModelRedeployCommand,
    ModelRedeployCompletedEvent,
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


TOPIC_PROD_GATE_EVALUATE = _topic_with_suffix("prod-promotion-gate-evaluate.v1")
TOPIC_DEPLOY_PUBLISH = _topic_with_suffix("redeploy-deploy-publish.v1")
TOPIC_REDEPLOY_COMPLETED = _topic_with_suffix("redeploy-completed.v1")


class HandlerRedeployOrchestrator:
    """Canonical orchestrator: dispatch redeploy commands over the bus."""

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Route a redeploy lifecycle event to the next bus command.

        ``redeploy-start`` -> prod-gate-evaluate command.
        ``prod-promotion-gate-evaluated`` -> deploy-publish command (allowed) or
        redeploy-completed:BLOCKED (denied).
        """
        event_type = envelope.event_type or ""
        correlation_id = envelope.correlation_id or uuid4()

        if event_type.endswith("prod-promotion-gate-evaluated.v1"):
            events = self._on_gate_evaluated(envelope, correlation_id)
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
        """Emit the prod-gate-evaluate command for a redeploy-start request."""
        start = _coerce_start(envelope.payload, correlation_id)
        # OMN-13436: the promotion grant is resolved OUT-OF-BAND (Phase-2b resolver
        # EFFECT) and is NEVER carried from the start request — a request cannot
        # author the authorization that approves it. The gate command leaves
        # ``promotion_grant`` and ``evaluated_at`` unset here; the resolver/runtime
        # stamps them before the gate compute runs. ``requested_by`` IS threaded so
        # the gate can enforce ``approved_by != requested_by``.
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
        )
        return [
            ModelEventEnvelope(
                payload=gate_command,
                correlation_id=start.correlation_id,
                event_type=TOPIC_PROD_GATE_EVALUATE,
            )
        ]

    def _on_gate_evaluated(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        """React to the gate decision: deploy if allowed, else complete BLOCKED."""
        decision, start = _coerce_gate_result(envelope.payload, correlation_id)

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

        publish_command = ModelDeployPublishCommand(
            correlation_id=correlation_id,
            scope=start.scope,
            git_ref=start.git_ref,
            runtime_lane=start.runtime_lane,
            build_source=start.build_source,
            services=start.services,
            image_ref=start.image_ref,
            image_digest=decision.image_digest or start.image_digest,
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
) -> tuple[ModelProdPromotionGateDecision, ModelRedeployStartCommand]:
    """Coerce the gate-evaluated payload into (decision, original start command).

    The runtime delivers the gate-evaluated event whose payload carries the
    ``ModelProdPromotionGateDecision`` and the echoed original start request so
    the orchestrator can build the deploy command without rehydrating state.
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
    return decision, start


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
    "TOPIC_PROD_GATE_EVALUATE",
    "TOPIC_REDEPLOY_COMPLETED",
    "HandlerRedeployOrchestrator",
]
