# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-seam reading row and the append request (OMN-22909)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_seam_reading_compute.models.model_required_sender import (
    EnumSeamTrigger,
)


class EnumRowAppendStatus(StrEnum):
    APPENDED = "appended"
    ALREADY_APPENDED = "already-appended"
    REFUSED_REWRITE = "refused-rewrite"


def row_key(seam: str, probed_at: datetime) -> str:
    """The identity of a row: one seam read at one instant."""
    stamp = probed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return hashlib.sha256(f"{seam}|{stamp}".encode()).hexdigest()[:32]


class ModelSeamRow(BaseModel):
    """One seam's reading at one instant; once stored it is never changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    seam: str = Field(min_length=1)
    trigger: EnumSeamTrigger
    verdict: EnumReadingVerdict
    reason: str = Field(min_length=1)
    probed_at: datetime

    @classmethod
    def of(
        cls,
        *,
        seam: str,
        trigger: EnumSeamTrigger,
        verdict: EnumReadingVerdict,
        reason: str,
        probed_at: datetime,
    ) -> Self:
        return cls(
            key=row_key(seam, probed_at),
            seam=seam,
            trigger=trigger,
            verdict=verdict,
            reason=reason,
            probed_at=probed_at,
        )


class ModelSeamRowAppendRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    rows: tuple[ModelSeamRow, ...] = Field(min_length=1)


class ModelSeamRowAppendResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    statuses: tuple[EnumRowAppendStatus, ...]
    appended: int
    already_appended: int
    refused: int
    refused_keys: tuple[str, ...] = ()


__all__ = [
    "EnumRowAppendStatus",
    "ModelSeamRow",
    "ModelSeamRowAppendRequest",
    "ModelSeamRowAppendResult",
    "row_key",
]
