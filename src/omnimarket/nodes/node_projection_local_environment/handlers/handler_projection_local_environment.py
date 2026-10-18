# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure plan: one environment report and its mode's stored rows in, the rows to write out.

It is the same decision the local store applies (``plan_local_environment_write``),
so the local SQLite path and the Postgres writer cannot judge differently. It
reads no clock of its own beyond the write stamp and keeps no state.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from omnimarket.handlers.handler_local_environment_store import (
    plan_local_environment_write,
)
from omnimarket.models.model_local_environment_status import (
    ModelLocalEnvironmentStatus,
)
from omnimarket.models.model_local_service_health import ModelLocalAlertRules


class HandlerProjectionLocalEnvironment:
    def handle(
        self,
        report: ModelLocalEnvironmentStatus,
        stored_rows: Sequence[Mapping[str, object]],
        rules: ModelLocalAlertRules,
        updated_at: datetime | None = None,
    ) -> list[dict[str, object]]:
        return plan_local_environment_write(
            report, stored_rows, rules, updated_at or datetime.now(UTC)
        )


__all__ = ["HandlerProjectionLocalEnvironment"]
