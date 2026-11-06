# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure plan: one alert transition and its stored row in, the row to write out.

It is the fold the local SQLite store applies (``plan_local_alert_fold``), so the
local path and the Postgres writer cannot fold differently. It reads no clock
and keeps no state.
"""

from omnimarket.handlers.local_alert_fold import plan_local_alert_fold
from omnimarket.nodes.node_projection_local_alerts.models.model_local_alerts_plan import (
    ModelLocalAlertsPlanRequest,
    ModelLocalAlertsPlanResponse,
)


class HandlerProjectionLocalAlerts:
    def handle(
        self, request: ModelLocalAlertsPlanRequest
    ) -> ModelLocalAlertsPlanResponse:
        return ModelLocalAlertsPlanResponse(
            row=plan_local_alert_fold(request.transition, request.stored_row)
        )


__all__ = ["HandlerProjectionLocalAlerts"]
