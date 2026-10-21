# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure plan: one environment report and its mode's stored rows in, the rows to write out.

It is the same decision the local store applies (``plan_local_environment_write``),
so the local SQLite path and the Postgres writer cannot judge differently. It
reads no clock of its own beyond the write stamp and keeps no state.
"""

from datetime import UTC, datetime

from omnimarket.handlers.handler_local_environment_store import (
    plan_local_environment_write,
)
from omnimarket.nodes.node_projection_local_environment.models.model_local_environment_plan import (
    ModelLocalEnvironmentPlanRequest,
    ModelLocalEnvironmentPlanResponse,
)


class HandlerProjectionLocalEnvironment:
    def handle(
        self, request: ModelLocalEnvironmentPlanRequest
    ) -> ModelLocalEnvironmentPlanResponse:
        rows = plan_local_environment_write(
            request.report,
            request.stored_rows,
            request.rules,
            request.updated_at or datetime.now(UTC),
        )
        return ModelLocalEnvironmentPlanResponse(rows=tuple(rows))


__all__ = ["HandlerProjectionLocalEnvironment"]
