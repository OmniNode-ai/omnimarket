# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command, gathered facts and result of one piece 11 reading run (OMN-22958)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_dev_deploy_reading import (
    ModelLabPassReceiptFact,
    ModelLineageFact,
    ModelRuntimeMerge,
    ModelRuntimeProbeFact,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class ModelDevDeployReadingCommand(BaseModel):
    """Run the reading; ``write`` false computes everything and appends nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    write: bool


class ModelDevDeployFacts(BaseModel):
    """What the sources gathered. A source that could not be read names why, and its facts are empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    merges: tuple[ModelRuntimeMerge, ...] = ()
    receipts: tuple[ModelLabPassReceiptFact, ...] = ()
    probes: tuple[ModelRuntimeProbeFact, ...] = ()
    #: What the clone says of the (merge, receipt) and (expected, observed) pairs; a pair it could not read says so.
    lineage: tuple[ModelLineageFact, ...] = ()
    receipts_unread_reason: str | None = None
    probes_unread_reason: str | None = None


class ModelDevDeployReadingResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    failure: str | None = None
    verdict: EnumReadingVerdict | None = None
    outcome: EnumReadingOutcome | None = None
    reasons: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    merges_checked: int = 0
    merges_receipted: int = 0
    probe_age_seconds: int | None = None
    row_key: str | None = None
    ledger: str | None = None
    row: str | None = None


__all__ = [
    "ModelDevDeployFacts",
    "ModelDevDeployReadingCommand",
    "ModelDevDeployReadingResult",
]
