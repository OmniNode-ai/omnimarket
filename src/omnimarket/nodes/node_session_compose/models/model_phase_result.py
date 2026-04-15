# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed phase execution result for node_session_compose."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["ModelPhaseResult"]

_ALLOWED_STATUSES = frozenset(
    {"dry_run", "dispatched", "succeeded", "failed", "skipped"}
)


class ModelPhaseResult(BaseModel):
    """Result of executing a single session phase."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: str = Field(
        ...,
        min_length=1,
        description="Phase identifier (e.g. 'platform_readiness', 'pipeline_fill')",
    )
    status: str = Field(
        ...,
        description="Phase execution status (dry_run, dispatched, succeeded, failed, skipped)",
    )

    @field_validator("phase")
    @classmethod
    def _validate_phase(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("phase must not be blank or whitespace-only")
        return stripped

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in _ALLOWED_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_ALLOWED_STATUSES)}, got {value!r}"
            )
        return value
