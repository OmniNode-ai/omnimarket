from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelEnvParityLaneVariableResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(..., min_length=1)
    variable_name: str = Field(..., min_length=1)
    present: bool = Field(description="True when the env var has a non-blank value.")
    fingerprint: str | None = Field(
        default=None,
        description="Redacted deterministic fingerprint for non-empty values.",
    )


class ModelEnvParityGap(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(..., min_length=1)
    variable_name: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    detail: str = Field(default="")


class ModelEnvParityComputeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(description="Result status: passed | gaps_detected | error")
    parity_ok: bool = Field(description="True when no parity gaps were detected.")
    scope: str = Field(..., min_length=1)
    lanes_checked: list[str] = Field(default_factory=list)
    variables_checked: list[str] = Field(default_factory=list)
    lane_results: list[ModelEnvParityLaneVariableResult] = Field(default_factory=list)
    gaps: list[ModelEnvParityGap] = Field(default_factory=list)
    settings_validation_errors: dict[str, list[str]] = Field(default_factory=dict)
    correlation_id: UUID | None = Field(default=None)
    error: str | None = Field(default=None)


__all__ = [
    "ModelEnvParityComputeResult",
    "ModelEnvParityGap",
    "ModelEnvParityLaneVariableResult",
]
