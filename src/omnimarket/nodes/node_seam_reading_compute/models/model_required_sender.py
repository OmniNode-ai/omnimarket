# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the required-sender reading (OMN-22909)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.model_reading import EnumReadingVerdict


class EnumSeamTrigger(StrEnum):
    """How a seam's command is meant to start: sent by a node on its own, or only when demanded."""

    AUTOMATIC = "automatic"
    ON_DEMAND = "on-demand"


class ModelSeamDeclaration(BaseModel):
    """One seam as declared: its trigger, whether it is required, and the node bound to send it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seam: str = Field(min_length=1)
    trigger: EnumSeamTrigger
    required: bool
    #: The node that sends an automatic seam's command; None when no sender is bound.
    sender: str | None = None

    @field_validator("sender")
    @classmethod
    def _sender_is_named(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("a blank sender is not a bound sender; use None")
        return value


class ModelDemandedVerdict(BaseModel):
    """What the attach-on-demand activation read for an on-demand seam."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seam: str = Field(min_length=1)
    verdict: EnumReadingVerdict


class ModelRequiredSenderRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    declarations: tuple[ModelSeamDeclaration, ...]
    demanded: tuple[ModelDemandedVerdict, ...] = ()
    now: datetime

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        names = [d.seam for d in self.declarations]
        if len(names) != len(set(names)):
            raise ValueError("a seam is declared twice")
        undeclared = sorted({d.seam for d in self.demanded} - set(names))
        if undeclared:
            raise ValueError(f"demand for undeclared seam: {undeclared[0]}")
        return self


class ModelSeamVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seam: str
    trigger: EnumSeamTrigger
    verdict: EnumReadingVerdict
    reason: str


class ModelSeamCounts(BaseModel):
    """The seam counts that ride on the piece's reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    automatic: int = Field(ge=0)
    on_demand: int = Field(ge=0)
    required_without_sender: int = Field(ge=0)
    working: int = Field(ge=0)
    not_working: int = Field(ge=0)
    unknown: int = Field(ge=0)


class ModelRequiredSenderResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: tuple[ModelSeamVerdict, ...]
    counts: ModelSeamCounts
    #: NOT_WORKING when any seam is; else UNKNOWN when any is; else WORKING.
    verdict: EnumReadingVerdict
    rendered_at: datetime


__all__ = [
    "EnumSeamTrigger",
    "ModelDemandedVerdict",
    "ModelRequiredSenderRequest",
    "ModelRequiredSenderResult",
    "ModelSeamCounts",
    "ModelSeamDeclaration",
    "ModelSeamVerdict",
]
