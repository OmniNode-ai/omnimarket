# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local environment health: one service's latest report, the rules, and the verdict.

These types are shared by the environment projection (which stores the latest
report per service) and the alert compute (which turns those rows and a time
into verdicts and open alerts), so neither node imports the other.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_local_alert_severity import EnumLocalAlertSeverity
from omnimarket.enums.enum_local_environment_mode import EnumLocalEnvironmentMode
from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.enums.enum_local_service_verdict import EnumLocalServiceVerdict


class ModelLocalServiceHealth(BaseModel):
    """The latest report held for one (tenant, mode, service)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    mode: EnumLocalEnvironmentMode
    service: str = Field(min_length=1)
    observed_state: EnumLocalServiceState
    reason: str = Field(min_length=1)
    last_seen: AwareDatetime
    # When the observed state last differed from the one before it.
    last_change: AwareDatetime


class ModelLocalAlertRule(BaseModel):
    """One rule: a verdict that opens an alert with this code and severity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    severity: EnumLocalAlertSeverity
    verdict: EnumLocalServiceVerdict


class ModelLocalAlertRules(BaseModel):
    """The compute's contract config: the freshness deadline and the rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    freshness_deadline_seconds: int = Field(ge=1)
    rules: tuple[ModelLocalAlertRule, ...]

    @model_validator(mode="after")
    def _one_rule_per_verdict(self) -> ModelLocalAlertRules:
        verdicts = [rule.verdict for rule in self.rules]
        if len(verdicts) != len(set(verdicts)):
            raise ValueError("each verdict may open at most one alert rule")
        if EnumLocalServiceVerdict.RUNNING in verdicts:
            raise ValueError("a RUNNING service never opens an alert")
        return self


class ModelLocalServiceVerdict(BaseModel):
    """One service's verdict at an observation time, and the alert it opens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    mode: EnumLocalEnvironmentMode
    service: str = Field(min_length=1)
    verdict: EnumLocalServiceVerdict
    reason: str = Field(min_length=1)
    alert_code: str | None = None
    alert_severity: EnumLocalAlertSeverity | None = None
    alert_since: AwareDatetime | None = None


class ModelLocalAlert(BaseModel):
    """One open alert: what it is, how urgent, which service, and since when."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    mode: EnumLocalEnvironmentMode
    service: str = Field(min_length=1)
    code: str = Field(min_length=1)
    severity: EnumLocalAlertSeverity
    since: AwareDatetime


__all__ = [
    "ModelLocalAlert",
    "ModelLocalAlertRule",
    "ModelLocalAlertRules",
    "ModelLocalServiceHealth",
    "ModelLocalServiceVerdict",
]
