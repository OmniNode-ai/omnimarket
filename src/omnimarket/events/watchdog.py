# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed FSM watchdog events — the platform terminal-state invariant vocabulary.

Platform invariant (OMN-12959): every workflow FSM must reach a terminal state
or trip a watchdog. When an orchestrator detects an unrecoverable or unroutable
condition it MUST emit a *typed* watchdog event — never a generic failure — so
projections and sweeps can distinguish failure classes for operator review.

The three canonical watchdog classes:

* ``workflow-timeout``    — the workflow ran past its archetype SLA without
  reaching a terminal state (started-but-no-terminal-event after SLA).
* ``workflow-unroutable`` — no eligible handler/tier/backend can advance the
  workflow (e.g. delegation escalation with no routable higher tier; the
  OMN-12939 / PR #1158 strand generalized).
* ``workflow-stalled``    — the workflow is alive but wedged: a non-terminal
  state with no forward progress and no path to advance (deadlock / wait on a
  signal that will never arrive).

These are distinct from a domain ``*-failed`` terminal: a watchdog event records
that the FSM could not reach *any* declared terminal on its own and was forced
terminal by the invariant. Operators review watchdog emissions to repair the
routing/SLA/wiring hole that produced them.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.topics import (
    TOPIC_WORKFLOW_STALLED,
    TOPIC_WORKFLOW_TIMEOUT,
    TOPIC_WORKFLOW_UNROUTABLE,
)

# ---------------------------------------------------------------------------
# Typed watchdog vocabulary
# ---------------------------------------------------------------------------


class EnumWatchdogEventType(StrEnum):
    """Canonical typed watchdog failure classes (operator review).

    NOT generic failures. Each value maps 1:1 to a canonical watchdog topic via
    :data:`WATCHDOG_TOPIC_BY_TYPE` so projections can filter by failure class.
    """

    WORKFLOW_TIMEOUT = "workflow-timeout"
    WORKFLOW_UNROUTABLE = "workflow-unroutable"
    WORKFLOW_STALLED = "workflow-stalled"


# Canonical watchdog topics resolved from the registry in ``events/topics.py``
# (the source of truth; OMN-12959). Orchestrators emitting a watchdog event
# publish to the topic resolved from the typed class; the stranded-workflow
# sweep treats these topics as terminal evidence so a tripped watchdog clears
# the strand. Re-exported here for callers that import from the watchdog module.
WATCHDOG_TOPIC_BY_TYPE: dict[EnumWatchdogEventType, str] = {
    EnumWatchdogEventType.WORKFLOW_TIMEOUT: TOPIC_WORKFLOW_TIMEOUT,
    EnumWatchdogEventType.WORKFLOW_UNROUTABLE: TOPIC_WORKFLOW_UNROUTABLE,
    EnumWatchdogEventType.WORKFLOW_STALLED: TOPIC_WORKFLOW_STALLED,
}

# The set of canonical watchdog topics; a member of this set published for a
# correlation id is terminal evidence for the stranded-workflow sweep.
WATCHDOG_TOPICS: frozenset[str] = frozenset(WATCHDOG_TOPIC_BY_TYPE.values())


def watchdog_topic_for(event_type: EnumWatchdogEventType) -> str:
    """Resolve the canonical topic for a typed watchdog event class.

    Fail-fast: raises ``KeyError`` for an unmapped class rather than returning a
    silent default, so a new enum member without a topic mapping is a hard error.
    """
    return WATCHDOG_TOPIC_BY_TYPE[event_type]


# ---------------------------------------------------------------------------
# Typed watchdog event payload
# ---------------------------------------------------------------------------


class ModelWatchdogEvent(BaseModel):
    """Typed terminal watchdog emission for a workflow FSM.

    Orchestrator base behavior constructs this when forcing an FSM terminal on
    an unrecoverable/unroutable/timeout condition. The ``event_type`` selects the
    publication topic via :func:`watchdog_topic_for`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: EnumWatchdogEventType = Field(
        description="Typed watchdog failure class (operator review)."
    )
    correlation_id: UUID = Field(
        description="Workflow correlation id this watchdog terminates."
    )
    archetype: str = Field(
        description="Orchestrator/workflow archetype that stranded (e.g. delegation, build_loop)."
    )
    workflow_state: str = Field(
        description="Non-terminal FSM state the workflow was stranded in when the watchdog tripped."
    )
    reason: str = Field(
        description="Operator-facing explanation of why no declared terminal was reachable."
    )
    elapsed_ms: int = Field(
        ge=0,
        description="Milliseconds from workflow start to watchdog trip.",
    )

    @property
    def topic(self) -> str:
        """Canonical publication topic for this typed watchdog event."""
        return watchdog_topic_for(self.event_type)


__all__ = [
    "TOPIC_WORKFLOW_STALLED",
    "TOPIC_WORKFLOW_TIMEOUT",
    "TOPIC_WORKFLOW_UNROUTABLE",
    "WATCHDOG_TOPICS",
    "WATCHDOG_TOPIC_BY_TYPE",
    "EnumWatchdogEventType",
    "ModelWatchdogEvent",
    "watchdog_topic_for",
]
