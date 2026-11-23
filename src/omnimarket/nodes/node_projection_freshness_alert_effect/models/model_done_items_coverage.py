# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared done-items coverage check, its observation and its verdict (OMN-26435)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_projection_freshness_alert_effect.models.enum_freshness_state import (
    EnumFreshnessState,
)

_RELATION = r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$"


class ModelCoverageCheck(BaseModel):
    """Contract-declared done-items coverage check (OMN-26435)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str = Field(..., pattern=r"^[a-z_][a-z0-9_]*$")
    demand_relation: str = Field(..., pattern=_RELATION)
    supply_relation: str = Field(..., pattern=_RELATION)
    max_age_seconds: int = Field(..., gt=0, strict=True)
    lookback_seconds: int = Field(..., gt=0, strict=True)

    @model_validator(mode="after")
    def _validate_lookback_exceeds_max_age(self) -> ModelCoverageCheck:
        if self.lookback_seconds <= self.max_age_seconds:
            raise ValueError("lookback_seconds must exceed max_age_seconds")
        return self


class ModelCoverageObservation(BaseModel):
    """Observation of done-items coverage (OMN-26435)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    demand_done_tickets: int = Field(..., ge=0, strict=True)
    unprojected_tickets: int = Field(..., ge=0, strict=True)
    refused_rows: int = Field(0, ge=0, strict=True)


class ModelCoverageVerdict(BaseModel):
    """Verdict for done-items coverage (OMN-26435)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    state: EnumFreshnessState
    demand_done_tickets: int | None
    unprojected_tickets: int | None
    refused_rows: int | None = None
    error: str | None = None


__all__ = ["ModelCoverageCheck", "ModelCoverageObservation", "ModelCoverageVerdict"]
