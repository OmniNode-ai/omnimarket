from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelEnvParityCollectRequest(BaseModel):
    """Request for live runtime-lane env collection + parity evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID | None = Field(
        default=None, description="Runtime correlation ID stamped by the dispatcher."
    )
    scope: str = Field(
        default="runtime-lanes",
        description="Human-readable parity scope for the receipt.",
    )
    lanes: list[str] = Field(
        default_factory=list,
        description=(
            "Subset of contract-declared lanes to collect; empty means all "
            "contract lanes."
        ),
    )
    variable_names: list[str] = Field(
        default_factory=list,
        description="Subset of contract variables to check; empty means all.",
    )
    ssh_target: str | None = Field(
        default=None,
        description=(
            "ssh destination (user@host or ssh-config alias) of the lane host. "
            "When unset, resolved from the contract-declared env var. If "
            "neither is provided the handler fails fast: no live collection "
            "input was provided."
        ),
    )
    connect_timeout_seconds: int = Field(
        default=10,
        ge=1,
        le=120,
        description="ssh ConnectTimeout for each read-only probe.",
    )

    @field_validator("scope")
    @classmethod
    def _normalize_scope(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("scope must not be blank")
        return normalized

    @field_validator("ssh_target")
    @classmethod
    def _normalize_ssh_target(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


__all__ = ["ModelEnvParityCollectRequest"]
