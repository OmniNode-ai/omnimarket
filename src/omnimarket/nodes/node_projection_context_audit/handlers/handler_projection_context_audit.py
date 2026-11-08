# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold: one context-audit event to its row, keyed by its source offset."""

from __future__ import annotations

import json

from omnimarket.nodes.node_projection_context_audit.models import (
    ModelContextAuditProjectionRequest,
    ModelContextAuditRow,
)
from omnimarket.projection.runner import deterministic_correlation_id


class HandlerProjectionContextAudit:
    """Maps an audit event to the row the retired consumer module wrote.

    The row key is derived from the event's Kafka coordinates
    (topic, partition, offset), so replaying the same stream derives the same
    key for the same event and the writer's UPSERT keeps one row per event.
    Two distinct events with identical content keep two rows, as they did under
    the retired module.
    """

    def handle(
        self, request: ModelContextAuditProjectionRequest
    ) -> ModelContextAuditRow:
        event = request.event
        details = event.violation_details
        return ModelContextAuditRow(
            event_key=deterministic_correlation_id(
                request.source_topic, request.source_partition, request.source_offset
            ),
            task_id=event.task_id,
            parent_task_id=event.parent_task_id,
            correlation_id=event.correlation_id,
            contract_id=event.contract_id,
            event_type=event.event_type,
            enforcement_level=event.enforcement_level,
            enforcement_action=event.enforcement_action,
            violation_details=(
                None
                if details is None
                else json.dumps(details, sort_keys=True, separators=(",", ":"))
            ),
            context_tokens_used=event.context_tokens_used,
            context_budget_tokens=event.context_budget_tokens,
            return_tokens=event.return_tokens,
            return_max_tokens=event.return_max_tokens,
        )


__all__ = ["HandlerProjectionContextAudit"]
