# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local alert compute's request and result."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict

from omnimarket.models.model_local_service_health import (
    ModelLocalAlert,
    ModelLocalAlertRules,
    ModelLocalServiceHealth,
    ModelLocalServiceVerdict,
)


class ModelLocalAlertRequest(BaseModel):
    """Every service's latest report, the time to judge them at, and the rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    services: tuple[ModelLocalServiceHealth, ...]
    observed_at: AwareDatetime
    rules: ModelLocalAlertRules


class ModelLocalAlertResult(BaseModel):
    """A verdict per service, in the request's order, and the alerts they open."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: tuple[ModelLocalServiceVerdict, ...]
    alerts: tuple[ModelLocalAlert, ...]


__all__ = ["ModelLocalAlert", "ModelLocalAlertRequest", "ModelLocalAlertResult"]
