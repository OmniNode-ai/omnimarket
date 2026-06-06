from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EnumEnvParityConsistency(StrEnum):
    PRESENCE = "presence"
    FINGERPRINT = "fingerprint"


class ModelEnvParityVariableRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, description="Environment variable name.")
    required_lanes: list[str] = Field(
        default_factory=list,
        description="Lanes where this variable must be present; empty means all contract lanes.",
    )
    consistency: EnumEnvParityConsistency = Field(
        default=EnumEnvParityConsistency.PRESENCE,
        description="How values are compared across lanes.",
    )
    category: str = Field(default="runtime", description="Rule grouping.")
    required_when: str | None = Field(
        default=None,
        description="Env flag that makes this variable required when set true.",
    )

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("variable name must not be blank")
        return normalized

    @field_validator("required_lanes")
    @classmethod
    def _normalize_required_lanes(cls, value: list[str]) -> list[str]:
        return _normalize_unique_text(value, "required_lanes")

    @field_validator("category")
    @classmethod
    def _normalize_category(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("category must not be blank")
        return normalized

    @field_validator("required_when")
    @classmethod
    def _normalize_required_when(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        return normalized or None


class ModelEnvParityContractConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lanes: list[str] = Field(..., min_length=1)
    variables: list[ModelEnvParityVariableRule] = Field(..., min_length=1)

    @field_validator("lanes")
    @classmethod
    def _normalize_lanes(cls, value: list[str]) -> list[str]:
        return _normalize_unique_text(value, "lanes")


class ModelEnvParityComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID | None = Field(
        default=None, description="Runtime correlation ID stamped by the dispatcher."
    )
    scope: str = Field(
        default="omnimarket",
        description="Scope of the environment parity check.",
    )
    lanes: list[str] = Field(
        default_factory=list,
        description="Subset of contract lanes to check; empty means all contract lanes.",
    )
    variable_names: list[str] = Field(
        default_factory=list,
        description="Subset of contract variables to check; empty means all variables.",
    )
    env_by_lane: dict[str, dict[str, str | None]] = Field(
        ...,
        min_length=1,
        description="Observed environment snapshot keyed by lane and env var name.",
    )

    @field_validator("scope")
    @classmethod
    def _normalize_scope(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("scope must not be blank")
        return normalized

    @field_validator("lanes")
    @classmethod
    def _normalize_lanes(cls, value: list[str]) -> list[str]:
        return _normalize_unique_text(value, "lanes")

    @field_validator("variable_names")
    @classmethod
    def _normalize_variable_names(cls, value: list[str]) -> list[str]:
        return [
            item.upper() for item in _normalize_unique_text(value, "variable_names")
        ]

    @field_validator("env_by_lane")
    @classmethod
    def _normalize_env_by_lane(
        cls, value: dict[str, dict[str, str | None]]
    ) -> dict[str, dict[str, str | None]]:
        normalized: dict[str, dict[str, str | None]] = {}
        for lane, env_map in value.items():
            lane_name = lane.strip()
            if not lane_name:
                raise ValueError("env_by_lane lane names must not be blank")
            normalized_env: dict[str, str | None] = {}
            for key, raw_value in env_map.items():
                env_name = key.strip().upper()
                if not env_name:
                    raise ValueError("env variable names must not be blank")
                normalized_env[env_name] = raw_value
            normalized[lane_name] = normalized_env
        return normalized


def _normalize_unique_text(value: list[str], field_name: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = item.strip()
        if not text:
            raise ValueError(f"{field_name} entries must not be blank")
        if text in seen:
            raise ValueError(f"{field_name} entries must be unique")
        normalized.append(text)
        seen.add(text)
    return normalized


__all__ = [
    "EnumEnvParityConsistency",
    "ModelEnvParityComputeRequest",
    "ModelEnvParityContractConfig",
    "ModelEnvParityVariableRule",
]
