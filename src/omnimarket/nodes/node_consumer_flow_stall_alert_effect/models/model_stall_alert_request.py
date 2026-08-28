# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B input for one stall evaluation (OMN-16778).

The window history arrives AS INPUT rather than being queried here, for the
same reason ``node_projection_consumer_flow`` resolves its upstream evidence in
the writer: it keeps ``handle()`` a pure function of its argument -- no clock,
no database, no ambient state -- so the same history always produces the same
decision and the hermetic tests drive the real handler rather than a stand-in.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.enum_consumer_flow_state import EnumConsumerFlowState
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models.model_stall_alert_policy import (
    ModelStallAlertPolicy,
)


class ModelFlowWindowObservation(BaseModel):
    """One materialized consumer-flow window row, as the alert sees it.

    The table name is deliberately not spelled here. This node never touches
    the database -- the history arrives as input, resolved by the caller that
    owns the read -- and ``scripts/generate_application_relation_inventory.py``
    derives its reader set by scanning node sources for relation-name tokens,
    so naming the relation in a docstring would record a read that does not
    happen.

    The counters are ``int | None`` and stay that way on purpose: an ``UNKNOWN``
    window carries ``None``, never ``0``. Coercing them to zero here would
    re-introduce, one layer up, the exact false-green OMN-16777 exists to close.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start: datetime
    window_end: datetime
    flow_state: EnumConsumerFlowState
    messages_in: int | None = None
    messages_out: int | None = None
    messages_dlq: int | None = None
    handler_errors: int | None = None


class ModelConsumerFlowStallAlertRequest(BaseModel):
    """One consumer's trailing window history plus the policy to judge it by."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer_group: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    node_id: UUID | None = Field(
        default=None,
        description="The runtime node whose heartbeat produced these windows.",
    )
    correlation_id: UUID = Field(
        ...,
        description="Correlation context carried through to the alert payload.",
    )
    windows: tuple[ModelFlowWindowObservation, ...] = Field(
        ...,
        min_length=1,
        description=(
            "Trailing window history, ordered OLDEST first. The caller supplies "
            "at least max(confirm_windows, clear_windows) windows; a shorter "
            "history simply cannot confirm, which is the correct answer rather "
            "than an assumed one."
        ),
    )
    policy: ModelStallAlertPolicy = Field(
        ...,
        description="Thresholds, loaded from contract.yaml by the caller.",
    )


__all__ = [
    "ModelConsumerFlowStallAlertRequest",
    "ModelFlowWindowObservation",
]
