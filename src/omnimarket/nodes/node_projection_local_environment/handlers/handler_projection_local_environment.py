# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure plan: one environment report and its mode's stored rows in, the rows to write out.

It is the same decision the local store applies (``plan_local_environment_write``),
so the local SQLite path and the Postgres writer cannot judge differently. It
reads no clock of its own beyond the write stamp and keeps no state.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.handlers.handler_local_environment_store import (
    plan_local_environment_write,
)
from omnimarket.models.model_local_environment_status import (
    ModelLocalEnvironmentStatus,
)
from omnimarket.nodes.node_projection_local_environment.models.model_local_environment_plan import (
    ModelLocalEnvironmentPlanRequest,
    ModelLocalEnvironmentPlanResponse,
)


def plan_local_environment_retirement(
    report: ModelLocalEnvironmentStatus,
    stored_rows: Sequence[Mapping[str, object]],
) -> Mapping[str, object] | None:
    """The stored row a retired report removes, or ``None`` when it holds none.

    Only a retired report removes anything, and only the row of its own tenant, mode
    and service. A service with no stored row is already forgotten, so a repeated
    report changes nothing. Every other row of the mode is left as it is.
    """
    if report.observed_state is not EnumLocalServiceState.RETIRED:
        return None
    return next(
        (
            row
            for row in stored_rows
            if str(row["tenant_id"]) == report.tenant_id
            and str(row["mode"]) == report.mode.value
            and str(row["service"]) == report.service
        ),
        None,
    )


class HandlerProjectionLocalEnvironment:
    def handle(
        self, request: ModelLocalEnvironmentPlanRequest
    ) -> ModelLocalEnvironmentPlanResponse:
        # A retired report writes no row: it removes one, which
        # ``plan_local_environment_retirement`` decides.
        if request.report.observed_state is EnumLocalServiceState.RETIRED:
            return ModelLocalEnvironmentPlanResponse(rows=())
        rows = plan_local_environment_write(
            request.report,
            request.stored_rows,
            request.rules,
            request.updated_at or datetime.now(UTC),
        )
        return ModelLocalEnvironmentPlanResponse(rows=tuple(rows))


__all__ = ["HandlerProjectionLocalEnvironment", "plan_local_environment_retirement"]
