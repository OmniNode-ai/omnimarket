# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, source read, reading and result of the bus login exposure probe."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingVerdict


class EnumBusReadFailure(StrEnum):
    """Why the login's view of the broker could not be read; every one reads UNKNOWN."""

    DESCRIBE_DENIED = "describe-denied"
    CREDENTIALS_MISSING = "credentials-missing"
    UNREADABLE = "unreadable"


class ModelBusLoginExposureRequest(BaseModel):
    """One probe: the correlation id and the time the read is judged at (no clock is read)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    now: datetime


class ModelBusVisibilityRead(BaseModel):
    """What a source read of the login's view returned, or why it could not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topics: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    read_at: datetime
    read_failure: EnumBusReadFailure | None = None


class ModelBusLoginExposureReading(BaseModel):
    """The login's visible topics and groups, and the ones the overlay does not allow it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumReadingVerdict
    visible_topics: tuple[str, ...]
    visible_groups: tuple[str, ...]
    exposed_topics: tuple[str, ...]
    exposed_groups: tuple[str, ...]
    read_at: datetime
    stale: bool
    read_failure: EnumBusReadFailure | None = None


class ModelBusLoginExposureResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    backend: Literal["inmemory", "redpanda"]
    reading: ModelBusLoginExposureReading


__all__ = [
    "EnumBusReadFailure",
    "ModelBusLoginExposureReading",
    "ModelBusLoginExposureRequest",
    "ModelBusLoginExposureResult",
    "ModelBusVisibilityRead",
]
