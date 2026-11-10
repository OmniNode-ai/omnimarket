# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the stability lane refresh: the dated release pin, the command and the result (OMN-21613)."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

RELEASE_TAG_PATTERN = r"^v[0-9]+\.[0-9]+\.[0-9]+$"


class EnumStabilityRefreshOutcome(StrEnum):
    """What one refresh request ended as."""

    PLANNED = "planned"
    ALREADY_PINNED = "already-pinned"
    REFRESHED = "refreshed"
    REFRESH_FAILED = "refresh-failed"
    REFUSED_PIN_DIFFERS = "refused-pin-differs"
    REFUSED_NOT_RELEASE_TAG = "refused-not-release-tag"
    REFUSED_PIN_UNREADABLE = "refused-pin-unreadable"


class ModelReleasePinSet(BaseModel):
    """One release tag per repository the lane stages. A dev tag, a sha or a branch is not a field value."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    omnibase_infra: str = Field(pattern=RELEASE_TAG_PATTERN)
    omnibase_core: str = Field(pattern=RELEASE_TAG_PATTERN)
    omnimarket: str = Field(pattern=RELEASE_TAG_PATTERN)

    def as_pairs(self) -> tuple[tuple[str, str], ...]:
        return (
            ("omnibase_infra", self.omnibase_infra),
            ("omnibase_core", self.omnibase_core),
            ("omnimarket", self.omnimarket),
        )


class ModelStabilityPinRecord(BaseModel):
    """The committed, dated release pin the cloud owner chooses and bumps."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: int = Field(ge=1)
    lane: str = Field(min_length=1)
    host: str = Field(min_length=1)
    date: date
    bumped_by: str = Field(min_length=1)
    pin: ModelReleasePinSet
    previous: ModelReleasePinSet | None = None


class ModelStabilityRefreshCommand(BaseModel):
    """Refresh the lane to its pin; ``requested`` names a different set and then needs ``bump``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    execute: bool = False
    bump: bool = False
    requested: ModelReleasePinSet | None = None


class ModelRunOutcome(BaseModel):
    """What the refresh process printed and returned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    return_code: int
    output_tail: str = ""


class ModelLaneHealthReading(BaseModel):
    """The lane's /health answer, three facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    environment: str
    wiring_healthy: bool


class ModelStabilityRefreshResult(BaseModel):
    """The typed answer: outcome, the exit code the CLI returns, the set run and what ran."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumStabilityRefreshOutcome
    exit_code: int
    lane: str
    box_id: str
    pin_date: str
    tags: tuple[tuple[str, str], ...] = ()
    running_tags: tuple[tuple[str, str], ...] = ()
    argv: tuple[str, ...] = ()
    env_names: tuple[str, ...] = ()
    pin_record_stale: bool = False
    detail: str = ""


__all__ = [
    "RELEASE_TAG_PATTERN",
    "EnumStabilityRefreshOutcome",
    "ModelLaneHealthReading",
    "ModelReleasePinSet",
    "ModelRunOutcome",
    "ModelStabilityPinRecord",
    "ModelStabilityRefreshCommand",
    "ModelStabilityRefreshResult",
]
