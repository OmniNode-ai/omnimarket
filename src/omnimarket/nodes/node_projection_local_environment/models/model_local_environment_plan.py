# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plan's typed input and output: one report and its mode's stored rows in, rows out."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_local_environment_status import (
    ModelLocalEnvironmentStatus,
)
from omnimarket.models.model_local_service_health import ModelLocalAlertRules


class ModelLocalEnvironmentPlanRequest(BaseModel):
    """A report, every stored row of its tenant's mode, the alert rules and the write stamp."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report: ModelLocalEnvironmentStatus
    stored_rows: tuple[dict[str, object], ...] = ()
    rules: ModelLocalAlertRules
    updated_at: datetime | None = None


class ModelLocalEnvironmentPlanResponse(BaseModel):
    """The rows to write; empty when the report changes nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[dict[str, object], ...] = ()


__all__ = ["ModelLocalEnvironmentPlanRequest", "ModelLocalEnvironmentPlanResponse"]
