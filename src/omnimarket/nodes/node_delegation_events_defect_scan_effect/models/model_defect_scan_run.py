# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of one run of the delegation_events defect scan (OMN-23331)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingVerdict


class ModelDefectScanCommand(BaseModel):
    """A correlation id and whether to write the row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    write: bool


class ModelDefectScanRunResult(BaseModel):
    """What one run read, what it concluded and what happened on the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["read", "failed"]
    verdict: EnumReadingVerdict
    reasons: tuple[str, ...]
    rows_scanned: int
    missing_terminal: int | None
    cost_unread_as_zero: int | None
    unattributed: int | None
    ledger: str
    row: str
    failure: str | None
    #: ``(topic, payload)`` pairs the run emitted, in order: a start, then one terminal.
    events: tuple[tuple[str, str], ...]
