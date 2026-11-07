# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator-owned persistent-state settings, validated on the first call."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelModelSwapStateConfig(BaseModel):
    """All local storage timing comes from the rendered node contract."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    directory: str = Field(min_length=1)
    sqlite_timeout_seconds: float = Field(gt=0)
    lock_poll_interval_seconds: float = Field(gt=0)

    @field_validator("directory")
    @classmethod
    def require_absolute_directory(cls, value: str) -> str:
        """Unrendered interpolation and relative paths cannot select state."""
        if value != value.strip() or "${" in value or not Path(value).is_absolute():
            raise ValueError("state.directory must be an absolute rendered path")
        return value
