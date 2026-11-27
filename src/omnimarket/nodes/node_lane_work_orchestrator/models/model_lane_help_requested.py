# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The help handler's input and answer (OMN-26722, plan 3.5 and 3.6).

``ModelLaneHelpRequested`` is the message the orchestrator's fold publishes on the help topic
when a workflow enters ``help_requested`` (task 6's ``ModelLaneWorkHelpMessage``); this handler
is subscribed to that topic by the orchestrator's contract. ``ModelLaneHelpDispatched`` says
which of the handler's two writes this call made.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_help_message import (
    ModelLaneWorkHelpMessage,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_ingress import (
    ModelLaneWorkIngress,
)


class ModelLaneHelpRequested(ModelLaneWorkHelpMessage):
    """The help topic's message, typed as this handler's input."""


class ModelLaneHelpDispatched(BaseModel):
    """What one call did. ``ledger_written`` and ``delegation_sent`` are the two writes; on a
    redelivery the delegation is not sent again when its action id is already on the topic.
    ``ingress`` is the typed input for the ingress topic when no tier is above the worker, so the
    workflow hears ``help_failed`` and ends instead of waiting for a request that was never sent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    action_id: str
    tier_name: str
    backend_id: str | None = None
    ledger_written: bool
    delegation_sent: bool
    ingress: ModelLaneWorkIngress | None = None


__all__: list[str] = ["ModelLaneHelpDispatched", "ModelLaneHelpRequested"]
