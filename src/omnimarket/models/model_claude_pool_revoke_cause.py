# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: models for the Claude token pool revoke cause decision."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class ModelRevokeCauseRequest(BaseModel):
    """One runner-written revoke to judge, with its evidence and declared markers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    evidence: str | None = None
    challenge_markers: tuple[str, ...] = ()

    @field_validator("challenge_markers")
    @classmethod
    def _challenge_markers_must_carry_text(
        cls, value: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        for marker in value:
            if not marker.strip():
                raise ValueError(f"{info.field_name} must not contain a blank marker")
        return value


class ModelRevokeCauseResult(BaseModel):
    """The cause decided for one runner-written revoke."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    cause: Literal["revoked", "challenged"]
    reason: str
