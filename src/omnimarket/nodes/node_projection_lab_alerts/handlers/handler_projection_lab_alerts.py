# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure plan: one lab alert transition and its stored row in, the row to write out.

A row is a function of the newest transition for its key, ordered by
(last_transition_at, last_transition_id): a replayed or older transition plans nothing, so the same
transitions in any order, any number of times, leave the same rows. It reads no clock and keeps no state.
A raise after a clear opens a new episode on the same row: the raise time moves and the clear time empties.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from omnimarket.enums.enum_lab_alert_transition import EnumLabAlertTransition
from omnimarket.models.model_lab_alert_transitioned import ModelLabAlertTransitioned
from omnimarket.nodes.node_projection_lab_alerts.models.model_lab_alerts_plan import (
    ModelLabAlertsPlanRequest,
    ModelLabAlertsPlanResponse,
)

STATE_OPEN = "open"
STATE_CLEARED = "cleared"


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromisoformat(str(value)).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def plan_lab_alert_fold(
    transition: ModelLabAlertTransitioned,
    stored: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """The row to UPSERT for a transition, or None when it is a replay or older."""
    if stored is not None:
        stored_order = (
            _as_datetime(stored["last_transition_at"]),
            str(stored["last_transition_id"]),
        )
        if (transition.occurred_at.astimezone(UTC), str(transition.transition_id)) <= (
            stored_order
        ):
            return None
    cleared = transition.transition is EnumLabAlertTransition.CLEARED
    return {
        "alert_id": transition.alert_id,
        "source": transition.source,
        "subject": transition.subject,
        "state": STATE_CLEARED if cleared else STATE_OPEN,
        "severity": transition.severity.value,
        "owner": transition.owner,
        "detail": transition.detail,
        "raised_at": _iso(transition.raised_at),
        "cleared_at": _iso(transition.occurred_at) if cleared else None,
        "last_transition_id": str(transition.transition_id),
        "last_transition_at": _iso(transition.occurred_at),
    }


class HandlerProjectionLabAlerts:
    def handle(self, request: ModelLabAlertsPlanRequest) -> ModelLabAlertsPlanResponse:
        return ModelLabAlertsPlanResponse(
            row=plan_lab_alert_fold(request.transition, request.stored_row)
        )


__all__ = [
    "STATE_CLEARED",
    "STATE_OPEN",
    "HandlerProjectionLabAlerts",
    "plan_lab_alert_fold",
]
