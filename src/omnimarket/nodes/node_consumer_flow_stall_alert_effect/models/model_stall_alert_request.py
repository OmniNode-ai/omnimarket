# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One assembled stall evaluation (OMN-16778).

This is no longer the node's declared ``input_model`` -- the redesign
(operator-approved 2026-08-28) made that the minimal
:class:`~omnimarket.nodes.node_consumer_flow_stall_alert_effect.models.model_stall_alert_trigger.ModelConsumerFlowStallAlertTrigger`,
because nothing on the platform produces a fully-assembled request and nothing
could.  The node assembles THIS internally, per key, and hands it to
:func:`~omnimarket.nodes.node_consumer_flow_stall_alert_effect.handlers.decide_stall_alert.decide_stall_alert`.

The window history still arrives AS INPUT to the decision rather than being
queried inside it, for the same reason ``node_projection_consumer_flow``
resolves its upstream evidence in the writer: it keeps the verdict a pure
function of its argument -- no clock, no database, no ambient state -- so the
same history always produces the same decision and the hermetic tests drive the
real decision code rather than a stand-in.  The read itself lives one layer out,
at the effect's own boundary (``handlers/flow_window_reader.py``).
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

    The relation these rows come from is named once, in the contract's
    ``windows_source`` block, and read at the effect boundary. Since the
    OMN-16778 redesign this node genuinely IS a reader of that relation, and
    ``scripts/generate_application_relation_inventory.py`` records it as one --
    which is now an accurate entry rather than the docstring artefact it was
    before the node did any reading.

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
