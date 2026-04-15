# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input command for node_session_compose."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["ModelSessionComposeCommand"]


class ModelSessionComposeCommand(BaseModel):
    """Command to compose a session from an ordered list of phases."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phases: list[str] = Field(
        ...,
        min_length=1,
        description="Ordered list of phase identifiers to compose",
    )
    dry_run: bool = Field(
        default=False,
        description="If True, return a dry-run plan without dispatching phases",
    )
    fail_fast: bool = Field(
        default=True,
        description="If True, stop on first phase failure; otherwise continue",
    )

    @field_validator("phases")
    @classmethod
    def _validate_phases(cls, value: list[str]) -> list[str]:
        for phase in value:
            if not phase or not phase.strip():
                raise ValueError("phase identifiers must not be blank")
        return value
