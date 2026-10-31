# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The run request and result of the process-health effect."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.process_health import EnumReadStatus, EnumSourceName


class ModelProcessHealthRunRequest(BaseModel):
    """One run: the environment to resolve paths from, the clock reading and an optional output path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environ: dict[str, str] = Field(
        description="The process environment, passed in, never read here."
    )
    generated_at: datetime
    output_override: str = Field(
        default="", description="An explicit output path; empty means the contract's."
    )

    @field_validator("generated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return value


class ModelProcessHealthRunSource(BaseModel):
    """One source and whether it was read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: EnumSourceName
    status: EnumReadStatus
    reason: str


class ModelProcessHealthRunResult(BaseModel):
    """Where the JSON was written and which sources were read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_path: str
    sources: tuple[ModelProcessHealthRunSource, ...]
