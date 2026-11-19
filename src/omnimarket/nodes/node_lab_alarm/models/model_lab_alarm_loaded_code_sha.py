# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's loaded-code-sha read."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumLoadedCodeShaOutcome(StrEnum):
    """READ: the sha the lane's deploy agent has loaded was read, with the surface that answered it. UNREAD: no surface delivered a sha."""

    READ = "READ"
    UNREAD = "UNREAD"


class ModelLoadedCodeShaRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)


class ModelLoadedCodeShaResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumLoadedCodeShaOutcome
    sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    surface: str | None = None
    #: Why the read is UNREAD; a READ carries none.
    error: str = ""

    @model_validator(mode="after")
    def _outcome_matches_evidence(self) -> ModelLoadedCodeShaResult:
        if self.outcome is EnumLoadedCodeShaOutcome.READ:
            if self.sha is None or self.surface is None or self.error != "":
                raise ValueError("a READ result carries a sha, a surface and no error")
            return self
        if self.sha is not None or not self.error:
            raise ValueError("an UNREAD result carries no sha and names the reason")
        return self


__all__ = [
    "EnumLoadedCodeShaOutcome",
    "ModelLoadedCodeShaRequest",
    "ModelLoadedCodeShaResult",
]
