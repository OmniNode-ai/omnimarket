# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed result envelope for the build-loop handler entry point.

``HandlerBuildLoop.handle()`` is the contract-declared runtime entry point.
The ``LocalRuntimeBusAdapter`` publish path only accepts a ``BaseModel``,
``dict``, or ``None`` return; a raw ``tuple`` return triggers
``ONEX_CORE_095_HANDLER_EXECUTION_ERROR`` (OMN-13841). This model wraps the
three FSM cycle products (final state, transition trace, completion event)
into a single typed, serializable receipt so the entry point returns a proper
``BaseModel``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_build_loop.models.model_loop_completed_event import (
    ModelLoopCompletedEvent,
)
from omnimarket.nodes.node_build_loop.models.model_loop_state import (
    ModelLoopState,
)
from omnimarket.nodes.node_build_loop.models.model_phase_transition_event import (
    ModelPhaseTransitionEvent,
)


class ModelBuildLoopResult(BaseModel):
    """Typed receipt returned by ``HandlerBuildLoop.handle()``.

    Carries the terminal FSM state, the ordered phase-transition trace, and the
    completion event. Serializable via ``model_dump_json()`` so the runtime bus
    adapter can publish it to the declared terminal topic.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    final_state: ModelLoopState
    transition_events: list[ModelPhaseTransitionEvent] = Field(default_factory=list)
    completed_event: ModelLoopCompletedEvent


__all__: list[str] = ["ModelBuildLoopResult"]
