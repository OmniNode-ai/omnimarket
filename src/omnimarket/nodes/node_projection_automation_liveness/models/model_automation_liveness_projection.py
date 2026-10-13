# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the automation-liveness fold."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from omnimarket.models.liveness.model_automation_liveness import (
    ModelAutomationAlarmCleared,
    ModelAutomationAlarmDelivered,
    ModelAutomationAlarmRaised,
    ModelAutomationAlarmRecorded,
    ModelAutomationHeartbeat,
    ModelAutomationLivenessDeclared,
    ModelAutomationLivenessVerdictEvent,
    ModelAutomationRunObserved,
)
from omnimarket.nodes.node_projection_automation_liveness.models.model_automation_liveness_rows import (
    ModelAutomationLivenessSnapshot,
)

#: The trailing window of ``failures_in_window``, in seconds.
DEFAULT_FAILURE_WINDOW_SECONDS = 24 * 60 * 60

#: Any one of the eight events of the seam.
AutomationLivenessEvent = (
    ModelAutomationRunObserved
    | ModelAutomationHeartbeat
    | ModelAutomationLivenessDeclared
    | ModelAutomationLivenessVerdictEvent
    | ModelAutomationAlarmRaised
    | ModelAutomationAlarmCleared
    | ModelAutomationAlarmDelivered
    | ModelAutomationAlarmRecorded
)


class ModelAutomationLivenessFoldRequest(BaseModel):
    """One seam event and the rows it can touch.

    ``prior`` holds the existing rows of the processes and episodes the event
    names (the writer reads them; a test builds them by hand). Rows outside it
    are treated as absent, so the caller must pass every row the event can
    change, including the run history of the process a run event names.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: AutomationLivenessEvent
    prior: ModelAutomationLivenessSnapshot = Field(
        default_factory=ModelAutomationLivenessSnapshot
    )
    failure_window_seconds: PositiveInt = DEFAULT_FAILURE_WINDOW_SECONDS


class ModelAutomationLivenessFoldResult(BaseModel):
    """The rows the event changed. A replayed event changes none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    changes: ModelAutomationLivenessSnapshot = Field(
        default_factory=ModelAutomationLivenessSnapshot
    )

    @property
    def row_count(self) -> int:
        c = self.changes
        return len(c.states) + len(c.runs) + len(c.episodes)


__all__ = [
    "DEFAULT_FAILURE_WINDOW_SECONDS",
    "AutomationLivenessEvent",
    "ModelAutomationLivenessFoldRequest",
    "ModelAutomationLivenessFoldResult",
]
