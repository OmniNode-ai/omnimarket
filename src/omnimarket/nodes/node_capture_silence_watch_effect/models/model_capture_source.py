# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One contract-declared capture source and its silence bounds."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelCaptureSource(BaseModel):
    """Identifiers are validated before interpolation; patterns stay SQL values."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_id: str = Field(..., pattern=r"^[a-z_][a-z0-9_]*$")
    relation: str = Field(..., pattern=r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")
    timestamp_column: str = Field(
        ..., pattern=r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$"
    )
    actor_column: str = Field(..., pattern=r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")
    actor_pattern: str | None
    actor_silence_seconds: int = Field(..., gt=0, strict=True)
    source_stale_seconds: int = Field(..., gt=0, strict=True)

    @field_validator("actor_pattern")
    @classmethod
    def _one_capture_group(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                pattern = re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid actor_pattern: {exc}") from exc
            if pattern.groups != 1 or "(?" in value:
                raise ValueError(
                    "actor_pattern must be a POSIX regex with one capture group"
                )
        return value
