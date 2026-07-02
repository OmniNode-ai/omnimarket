# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed result envelope for the design-to-plan handler entry point.

``HandlerDesignToPlan.handle()`` is the contract-declared runtime entry point.
The ``LocalRuntimeBusAdapter`` publish path only accepts a ``BaseModel``,
``dict``, or ``None`` return; a raw ``tuple`` return triggers
``ONEX_CORE_095_HANDLER_EXECUTION_ERROR`` (OMN-13841). This model wraps the
three FSM pipeline products (final state, phase-event trace, completion event)
into a single typed, serializable receipt so the entry point returns a proper
``BaseModel``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_state import (
    ModelDesignToPlanCompletedEvent,
    ModelDesignToPlanPhaseEvent,
    ModelDesignToPlanState,
)


class ModelDesignToPlanResult(BaseModel):
    """Typed receipt returned by ``HandlerDesignToPlan.handle()``.

    Carries the terminal FSM state, the ordered phase-event trace, and the
    completion event. Serializable via ``model_dump_json()`` so the runtime bus
    adapter can publish it to the declared terminal topic.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    final_state: ModelDesignToPlanState
    phase_events: list[ModelDesignToPlanPhaseEvent] = Field(default_factory=list)
    completed_event: ModelDesignToPlanCompletedEvent


__all__: list[str] = ["ModelDesignToPlanResult"]
