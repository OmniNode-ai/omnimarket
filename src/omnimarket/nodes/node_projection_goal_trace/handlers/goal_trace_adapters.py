# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Source events to content-free trace events (OMN-20034, GC.11).

Three sources feed the trace, and each is reduced to identifiers, times and a
digest. No prompt, tool input, tool result or reply text crosses this module.

* a captured hook event (the stamped binding event carries the goal, GC.8);
* a goal-scoped verification result, which is one append-only attempt;
* an already normalized trace event (claim, revision, close, delegation,
  receipt), published by the producers of those events.

The classification is by payload shape and not by topic, so the contract is the
only place a topic is named.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict

from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_projection_dod_verdict import (
    resolve_dod_eval_outcome,
)
from omnimarket.nodes.node_projection_dod_verdict.models import ModelDodVerdictWire
from omnimarket.nodes.node_projection_goal_trace.models import (
    EnumGoalTraceEventKind,
    ModelGoalTraceEvent,
)

MAIN_THREAD_AGENT = "main"


def _uuid_or_none(value: object) -> UUID | None:
    return UUID(value) if isinstance(value, str) else None


class ModelGoalTraceHookLineage(BaseModel):
    """The lineage keys of a captured hook event that the trace joins on."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    session_id: str
    agent_id: str | None = None
    tool_use_id: str | None = None


class ModelGoalTraceHookWire(BaseModel):
    """A captured hook event, read for identity, time, agent and goal stamp only."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: UUID
    hook_event_name: str
    emitted_at: AwareDatetime
    lineage: ModelGoalTraceHookLineage
    # The producer stamps the goal in the payload of the one binding event
    # (GC.8); every other event leaves both keys out.
    payload: dict[str, Any] = {}

    @property
    def goal_id(self) -> UUID | None:
        return _uuid_or_none(self.payload.get("goal_id"))

    @property
    def parent_goal_id(self) -> UUID | None:
        return _uuid_or_none(self.payload.get("parent_goal_id"))


def agent_key(session_id: str, agent_id: str | None) -> str:
    """A session's main thread is an agent too: subagents share its session id."""
    return f"{session_id}:{agent_id or MAIN_THREAD_AGENT}"


def digest_of(parts: dict[str, Any]) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def classify(data: dict[str, Any]) -> str:
    """Which source a payload is, by its own keys. Raises on none of them."""
    if "body_digest" in data and "kind" in data:
        return "trace"
    if "hook_event_name" in data and "lineage" in data:
        return "hook"
    if "ticket_id" in data and "completed_at" in data and "correlation_id" in data:
        return "verdict"
    raise ValueError(
        "payload is neither a trace event, a hook event nor a verification result"
    )


def from_hook(data: dict[str, Any]) -> ModelGoalTraceEvent:
    wire = ModelGoalTraceHookWire.model_validate(data)
    lineage = wire.lineage
    return ModelGoalTraceEvent(
        event_id=str(wire.event_id),
        kind=EnumGoalTraceEventKind.HOOK,
        body_digest=digest_of(
            {
                "event_id": str(wire.event_id),
                "hook": wire.hook_event_name,
                "emitted_at": wire.emitted_at.isoformat(),
                "session": lineage.session_id,
                "agent": lineage.agent_id,
                "tool_use_id": lineage.tool_use_id,
                "goal_id": str(wire.goal_id) if wire.goal_id else None,
            }
        ),
        emitted_at=wire.emitted_at,
        goal_id=wire.goal_id,
        parent_goal_id=wire.parent_goal_id,
        session_id=lineage.session_id,
        agent_key=agent_key(lineage.session_id, lineage.agent_id),
    )


def attempt_id_of(wire: ModelDodVerdictWire) -> str:
    """One verification run: its correlation id and completion time (the row key)."""
    return f"{wire.correlation_id}@{wire.completed_at.isoformat()}"


def from_verdict(data: dict[str, Any]) -> ModelGoalTraceEvent | None:
    """One attempt, or None for a run that is not goal-scoped or only a rehearsal."""
    wire = ModelDodVerdictWire.model_validate(data)
    if wire.goal_id is None or wire.dry_run:
        return None
    outcome = resolve_dod_eval_outcome(
        status=wire.status,
        failed_count=wire.failed_count,
        total_checks=wire.total_checks,
        behavior_proving_count=wire.behavior_proving_count,
    ).outcome
    return ModelGoalTraceEvent(
        event_id=attempt_id_of(wire),
        kind=EnumGoalTraceEventKind.ATTEMPT,
        body_digest=digest_of(
            {
                "attempt": attempt_id_of(wire),
                "goal_id": str(wire.goal_id),
                "revision": str(wire.contract_revision)
                if wire.contract_revision
                else None,
                "status": wire.status.value,
                "outcome": outcome.value,
                "failed": wire.failed_count,
                "total": wire.total_checks,
                "behavior": wire.behavior_proving_count,
            }
        ),
        emitted_at=wire.completed_at,
        goal_id=wire.goal_id,
        parent_goal_id=wire.parent_goal_id,
        revision=str(wire.contract_revision) if wire.contract_revision else None,
        completed_at=wire.completed_at,
        verdict=outcome.value,
    )


def to_trace_event(data: dict[str, Any]) -> ModelGoalTraceEvent | None:
    """Reduce one consumed payload to a trace event, or None when out of scope."""
    source = classify(data)
    if source == "trace":
        return ModelGoalTraceEvent.model_validate(data)
    if source == "hook":
        return from_hook(data)
    return from_verdict(data)


__all__ = [
    "MAIN_THREAD_AGENT",
    "ModelGoalTraceHookLineage",
    "ModelGoalTraceHookWire",
    "agent_key",
    "attempt_id_of",
    "classify",
    "digest_of",
    "from_hook",
    "from_verdict",
    "to_trace_event",
]
