# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The engine a lab-fill lane was routed to, and why (OMN-20287).

The fill asks the routing order node for each work item's engine order; the answer is
injected into the dispatch plan request as ``routing_orders``. Every placed lane records
the order it asked for, the engine it took and the reason, as a delegation event the
dashboard reads.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

_CAMEL = ConfigDict(
    frozen=True, extra="forbid", alias_generator=to_camel, populate_by_name=True
)

ROUTING_EVENT_TYPE = "lab-fill-routing-decision"


class ModelLabFillRoutingSkip(BaseModel):
    """An engine of the order that could not take the lane, with the reason."""

    model_config = _CAMEL

    engine: str
    reason: str


class ModelLabFillRoutingDecision(BaseModel):
    """One lane's routing decision: requested order, chosen engine, reason."""

    model_config = _CAMEL

    task_class: str
    band: str = ""
    requested_order: tuple[str, ...]
    chosen_engine: str
    reason: str
    skipped: tuple[ModelLabFillRoutingSkip, ...] = ()


class ModelLabFillRoutingEvent(ModelLabFillRoutingDecision):
    """The decision as a delegation event: the lane and ticket it belongs to."""

    event_type: str = Field(default=ROUTING_EVENT_TYPE)
    lane: str
    ticket: str
    host: str
