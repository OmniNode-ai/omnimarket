# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of one run of the delegation completion-rate reading (OMN-23108)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingVerdict


class ModelDelegationRateCommand(BaseModel):
    """A correlation id and whether to write the row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    write: bool


class ModelDelegationRateRunResult(BaseModel):
    """What one run read, what it concluded and what happened on the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["read", "failed"]
    verdict: EnumReadingVerdict
    reasons: tuple[str, ...]
    r_all: int | None
    r_dep: int | None
    failed_rate: float | None
    rows_in_window: int
    ledger: str
    row: str
    failure: str | None
    #: ``(topic, payload)`` pairs the run emitted, in order: a start, then one terminal.
    events: tuple[tuple[str, str], ...]
