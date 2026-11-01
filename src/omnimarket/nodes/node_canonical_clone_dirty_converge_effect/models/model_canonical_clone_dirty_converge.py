# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the dirty-converge tick declares and what it did to one clone (OMN-21821)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumDirtyConvergeStatus(StrEnum):
    CONVERGED = "converged"
    SKIPPED = "skipped"
    FAILED = "failed"


class ModelDirtyConvergeDeclaration(BaseModel):
    """The ``dirty_converge`` section of the node contract, validated once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str
    converge_script: str
    ticket: str = Field(pattern=r"^OMN-\d+$")
    lane: str
    tracking_branches: tuple[str, ...] = Field(min_length=1)
    max_listed: int = Field(ge=1)

    @model_validator(mode="after")
    def _relative(self) -> Self:
        for path in (self.engine, self.converge_script):
            if path.startswith("/") or ".." in path.split("/"):
                raise ValueError(
                    "engine and converge_script are registry-relative paths"
                )
        return self


class ModelDirtyConvergeOutcome(BaseModel):
    """One refused clone the tick looked at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    clone: str
    status: EnumDirtyConvergeStatus
    reason: str = Field(default="", max_length=2000)
    refused_for_seconds: int = Field(ge=0)
    dropped_paths: tuple[str, ...] = ()
    dropped_commits: tuple[str, ...] = ()
    converged_to: str = ""


class ModelDirtyConvergeTickRequest(BaseModel):
    """One tick over the registry root the handler is composed with; it carries no input."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelDirtyConvergeTickReceipt(BaseModel):
    """Every refused clone the tick acted on or left alone, with the age it applied."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    after_seconds: int = Field(ge=1)
    outcomes: tuple[ModelDirtyConvergeOutcome, ...] = ()


__all__ = [
    "EnumDirtyConvergeStatus",
    "ModelDirtyConvergeDeclaration",
    "ModelDirtyConvergeOutcome",
    "ModelDirtyConvergeTickReceipt",
    "ModelDirtyConvergeTickRequest",
]
