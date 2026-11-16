# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B label-record operation; publication belongs to the runtime."""

from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.events.delegation_eval import (
    ModelDelegationEvalItemLabelled,
    ModelLabelRecordRequest,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.models import (
    ModelLabelRecordResult,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.protocols import (
    ProtocolDelegationEventSnapshot,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.scrubber import scrub_snapshot
from omnimarket.nodes.node_delegation_eval_orchestrator.sources.source_tenant_event_snapshot import (
    snapshots_for_tenant,
)


class HandlerDelegationEvalOrchestrator:
    """Snapshot and scrub one label; holds no workflow or envelope state."""

    def __init__(
        self,
        snapshot_source: ProtocolDelegationEventSnapshot | None = None,
        *,
        snapshot_source_factory: Callable[[str], ProtocolDelegationEventSnapshot]
        | None = None,
    ) -> None:
        self._snapshot_source = snapshot_source
        self._snapshot_source_factory = snapshot_source_factory or snapshots_for_tenant

    def handle(self, request: ModelLabelRecordRequest) -> ModelLabelRecordResult:
        source = self._snapshot_source
        if source is None:
            # One source per command, bound to the command's tenant (OMN-20577).
            source = self._snapshot_source_factory(str(request.tenant_id))
        snapshot = source.get_snapshot(request.correlation_id, request.attempt_index)
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
        return ModelLabelRecordResult(payload=payload)
