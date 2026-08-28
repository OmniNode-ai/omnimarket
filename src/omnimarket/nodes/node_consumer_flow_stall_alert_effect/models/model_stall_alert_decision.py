# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B output of one stall evaluation (OMN-16778)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.enum_consumer_flow_state import EnumConsumerFlowState
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models.enum_stall_alert_outcome import (
    EnumStallAlertOutcome,
)
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models.enum_stall_alert_severity import (
    EnumStallAlertSeverity,
)


class ModelStallAlertPayload(BaseModel):
    """Everything a human needs to act, carried in the alert itself.

    OMN-16778 AC4: *falsified by a message that requires a human to go run*
    ``rpk`` *to find out what broke.* So the payload names the consumer, the
    topic, what went in, what came out, what was dead-lettered, how long it has
    been that way, and the correlation context -- not "something is wrong",
    which is the generic-error failure the omnidash bridge already commits.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer_group: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    flow_state: EnumConsumerFlowState
    consecutive_windows: int = Field(..., ge=1)
    messages_in: int | None = None
    messages_out: int | None = None
    messages_dlq: int | None = None
    handler_errors: int | None = None
    window_start: datetime
    window_end: datetime
    node_id: UUID | None = None
    correlation_id: UUID


class ModelConsumerFlowStallAlertDecision(BaseModel):
    """What this evaluation concluded, and whether anything should be posted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer_group: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    outcome: EnumStallAlertOutcome
    severity: EnumStallAlertSeverity
    consecutive_alerting_windows: int = Field(..., ge=0)
    consecutive_unknown_windows: int = Field(..., ge=0)
    should_publish: bool = Field(
        ...,
        description=(
            "Whether a Slack command should be published. FAIL always "
            "publishes; a WARN publishes only when the contract declares "
            "deliver_warnings."
        ),
    )
    reason: str = Field(
        ...,
        min_length=1,
        description="Human-readable justification, carried into the terminal event.",
    )
    idempotency_key: str | None = Field(
        default=None,
        description=(
            "Renotify-bucketed key handed to node_slack_publish_effect. Its "
            "durable ledger collapses a repeat inside the same bucket, so this "
            "node needs no state file to avoid re-posting a standing stall."
        ),
    )
    alert: ModelStallAlertPayload | None = Field(
        default=None,
        description="Present exactly when should_publish is True.",
    )


__all__ = [
    "ModelConsumerFlowStallAlertDecision",
    "ModelStallAlertPayload",
]
