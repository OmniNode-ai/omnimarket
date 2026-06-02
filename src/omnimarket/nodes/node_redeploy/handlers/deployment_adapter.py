# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DeploymentAdapter protocol + Kafka-backed default for node_redeploy rollback.

OMN-9579 / OMN-12577. The workflow runner reaches the deploy agent ONLY through
``handler_redeploy_kafka``; rollback is the same boundary. ``ProtocolDeploymentAdapter``
defines the injectable rollback action so the FSM stays pure and the rollback
event is published over Kafka, never via direct Docker/subprocess.

A failed post-deploy health check (smoke / health / timeout) restores the
previous known-good image and emits
``onex.evt.omnimarket.redeploy-rolled-back.v1`` with the restored image and the
failure reason.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from omnimarket.nodes.node_redeploy.handlers.handler_redeploy_kafka import (
    TOPIC_REDEPLOY_ROLLED_BACK,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    EnumRuntimeLane,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_state import (
    EnumRedeployPhase,
    ModelRedeployRolledBackEvent,
)

# Previous known-good runtime image restored on rollback when the deployment did
# not carry an explicit prior digest. This is the documented last-good runtime
# tag; a real deployment overrides it via ``ModelRedeployState.previous_image``.
DEFAULT_PREVIOUS_IMAGE = "omninode-runtime:v2.3.1"


@runtime_checkable
class ProtocolDeploymentAdapter(Protocol):
    """Injectable deploy/rollback boundary for the redeploy workflow."""

    async def rollback(
        self,
        correlation_id: UUID,
        runtime_lane: EnumRuntimeLane,
        restored_image: str,
        failure_reason: str,
        failed_phase: EnumRedeployPhase,
    ) -> ModelRedeployRolledBackEvent:
        """Restore the previous image and publish the rolled-back event."""
        ...


class DeploymentAdapterKafka:
    """Kafka-backed ``ProtocolDeploymentAdapter``.

    Publishes the rolled-back event over the event bus. It performs no Docker or
    subprocess calls itself — restoring the image is the deploy agent's job,
    triggered by the rebuild command path; this adapter records and announces the
    rollback decision on the contract-declared topic.
    """

    def __init__(self, event_bus: Any) -> None:
        if event_bus is None:
            raise RuntimeError(  # error-ok: mis-wired constructor
                "DeploymentAdapterKafka requires an event_bus to publish the "
                "redeploy-rolled-back event."
            )
        self._bus: Any = event_bus

    async def rollback(
        self,
        correlation_id: UUID,
        runtime_lane: EnumRuntimeLane,
        restored_image: str,
        failure_reason: str,
        failed_phase: EnumRedeployPhase,
    ) -> ModelRedeployRolledBackEvent:
        event = ModelRedeployRolledBackEvent(
            correlation_id=correlation_id,
            runtime_lane=runtime_lane,
            restored_image=restored_image,
            failure_reason=failure_reason,
            failed_phase=failed_phase,
        )
        await self._bus.publish(
            TOPIC_REDEPLOY_ROLLED_BACK,
            key=str(correlation_id).encode(),
            value=json.dumps(event.model_dump(mode="json")).encode(),
        )
        return event


__all__: list[str] = [
    "DEFAULT_PREVIOUS_IMAGE",
    "TOPIC_REDEPLOY_ROLLED_BACK",
    "DeploymentAdapterKafka",
    "ProtocolDeploymentAdapter",
]
