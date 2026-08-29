# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The minimal trigger this node is actually sent (OMN-16778, redesign).

Why this model exists
---------------------
The first cut of this node declared ``ModelConsumerFlowStallAlertRequest`` — a
fully-assembled evaluation: one consumer, one topic, a trailing window history
and a policy — as its ``input_model``.  Nothing on the platform produces that
shape, and nothing ever could: the only publisher of
``onex.evt.omnimarket.projection-consumer-flow-applied.v1`` is the runtime's
generic projection emitter, which carries the projection handler's own return
value — a *batch* ack of the form ``{"rows_upserted": N, "flow_rows": [...],
"projected": true}``.

Live on the ``.201`` dev lane, 2026-08-29T01:3x, that mismatch read as::

    Dispatcher 'dispatcher.auto.consumer_flow_stall_alert_effect...' failed:
    ValidationError: 8 validation errors for ModelConsumerFlowStallAlertRequest
      consumer_group  Field required   [input_value={'rows_upserted': 309, ...}]
      topic           Field required
      correlation_id  Field required
      windows         Field required
      policy          Field required
      rows_upserted   Extra inputs are not permitted
      flow_rows       Extra inputs are not permitted
      projected       Extra inputs are not permitted

94 occurrences in a two-minute window, every one DLQ-routed.  The operator
approved the redesign on 2026-08-28 ("Approve redesign"): the node assembles its
own request — policy from its own ``contract.yaml``, window history read by the
node itself — and its declared input is only the trigger that says *new windows
have landed, go look*.

Why ``extra="ignore"`` here and nowhere else in this node
--------------------------------------------------------
Every other model in this package is ``extra="forbid"``.  This one is not, and
the asymmetry is deliberate.  This is the single *foreign* payload the node
receives: it is composed by ``handler_wiring._build_projection_terminal_payload``
in ``omnibase_infra``, which by its own docstring **adds** keys (``projected``
is exactly such an addition, kept for existing Pattern-B consumers).  Forbidding
unknown keys on a payload whose producer is documented to grow it would let an
additive, harmless upstream change silently kill the alert path again — which is
the failure class this whole node exists to catch.  Unknown keys are ignored;
the keys this node needs are all required.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.enum_consumer_flow_state import EnumConsumerFlowState


class ModelAppliedFlowRow(BaseModel):
    """One row out of the applied event's ``flow_rows`` batch.

    Only the identity of the row is read here — which consumer, which topic,
    which window, and the state the projection derived.  The counters are read
    back from the projection when the node assembles its own history, so this
    model is deliberately not a second copy of ``ModelConsumerFlowRow``: a
    partial mirror that drifts is worse than a narrow one that cannot.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    consumer_group: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    window_start: datetime
    flow_state: EnumConsumerFlowState
    node_id: UUID | None = Field(
        default=None,
        description="The runtime node whose heartbeat produced this window.",
    )


class ModelConsumerFlowStallAlertTrigger(BaseModel):
    """The applied-event payload, as this node's declared ``input_model``.

    This is a *trigger*, not a request: it names which (consumer_group, topic)
    keys just moved, and nothing else.  Thresholds come from this node's
    contract and the window history comes from the projection the node reads
    itself, so no caller has to know either — which is the whole point of the
    approved redesign.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    rows_upserted: int = Field(
        ...,
        ge=0,
        description=(
            "How many rows the projection wrote for this heartbeat window. "
            "Zero is a real answer (a priming tick, or a heartbeat carrying no "
            "window) and is not an error."
        ),
    )
    flow_rows: tuple[ModelAppliedFlowRow, ...] = Field(
        ...,
        description=(
            "The rows the projection just wrote. Empty when rows_upserted is 0. "
            "No default: an absent key means the producer changed shape, which "
            "must fail loudly rather than evaluate an empty batch."
        ),
    )
    projected: bool = Field(
        ...,
        description=(
            "The runtime emitter's ack key. Read here so a payload missing it "
            "is refused rather than mistaken for an applied event."
        ),
    )

    def alerting_keys(self) -> tuple[tuple[str, str], ...]:
        """The distinct (consumer_group, topic) keys this trigger touched.

        Order is the batch's own order with duplicates dropped, so the same
        trigger always evaluates the same keys in the same sequence and a
        replayed applied event reproduces the same evaluation.
        """
        seen: dict[tuple[str, str], None] = {}
        for row in self.flow_rows:
            seen.setdefault((row.consumer_group, row.topic), None)
        return tuple(seen)


__all__ = [
    "ModelAppliedFlowRow",
    "ModelConsumerFlowStallAlertTrigger",
]
