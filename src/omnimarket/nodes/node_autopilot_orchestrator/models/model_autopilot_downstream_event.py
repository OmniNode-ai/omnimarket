# SPDX-License-Identifier: MIT
"""Downstream result events the autopilot orchestrator advances on.

Each model is the autopilot's own read of one downstream node's result event.
The fields are the subset the orchestrator needs; ``extra="ignore"`` keeps a
downstream node free to grow its payload without breaking the autopilot, and
no downstream node's private model is imported (shared types are promoted, not
reached into).

``correlation_id`` is optional on the wire so a malformed event is dropped by
the handler (logged, no state change) instead of failing validation before the
handler runs.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelAutopilotDownstreamEvent(BaseModel):
    """Base for a downstream result event the autopilot correlates by run id."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: UUID | None = None


class ModelPrLifecycleCompletedEvent(ModelAutopilotDownstreamEvent):
    """pr-lifecycle-orchestrator-completed: phase B reached a result."""

    final_state: str = ""
    error_message: str | None = None


class ModelPrLifecycleFailedEvent(ModelAutopilotDownstreamEvent):
    """pr-lifecycle-orchestrator-failed: phase B's node raised."""

    error: str = ""


class ModelPlatformDiagnosticsCompletedEvent(ModelAutopilotDownstreamEvent):
    """platform-diagnostics-completed: phase C's infra verdict (PASS/WARN/FAIL)."""

    overall_status: str = ""


class ModelAislopSweepCompletedEvent(ModelAutopilotDownstreamEvent):
    """aislop-sweep-completed: phase D's sweep verdict."""

    status: str = ""


__all__: list[str] = [
    "ModelAislopSweepCompletedEvent",
    "ModelAutopilotDownstreamEvent",
    "ModelPlatformDiagnosticsCompletedEvent",
    "ModelPrLifecycleCompletedEvent",
    "ModelPrLifecycleFailedEvent",
]
