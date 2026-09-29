# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B label-record operation; publication belongs to the runtime."""

from datetime import UTC, datetime

from omnimarket.nodes.node_delegation_eval_orchestrator.models import (
    ModelDelegationEvalItemLabelled,
    ModelLabelRecordRequest,
    ModelLabelRecordResult,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.protocols import (
    ProtocolDelegationEventSnapshot,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.scrubber import scrub_snapshot

_LABEL_TOPIC = "onex.evt.omnimarket.delegation-eval-item-labelled.v1"  # onex-topic-allow: declared in this node's publish_topics


class HandlerDelegationEvalOrchestrator:
    """Snapshot and scrub one label; holds no workflow or envelope state."""

    def __init__(self, snapshot_source: ProtocolDelegationEventSnapshot) -> None:
        self._snapshot_source = snapshot_source

    def handle(self, request: ModelLabelRecordRequest) -> ModelLabelRecordResult:
        snapshot = self._snapshot_source.get_snapshot(
            request.correlation_id, request.attempt_index
        )
        # Redact BOTH texts before constructing any outbound payload.
        prompt = scrub_snapshot(snapshot.prompt)
        response = scrub_snapshot(snapshot.response)
        payload = ModelDelegationEvalItemLabelled(
            **request.model_dump(),
            prompt_snapshot=prompt,
            response_snapshot=response,
            task_class=snapshot.task_class,
            gate_verdict=snapshot.gate_verdict,
            deciding_check=snapshot.deciding_check,
            observed_at=datetime.now(UTC),
        )
        return ModelLabelRecordResult(topic=_LABEL_TOPIC, payload=payload)
