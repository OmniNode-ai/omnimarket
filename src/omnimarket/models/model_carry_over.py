# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Carry-over proposals: closure facts, confirmed labels and a bounded operator view."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_ask_state import ModelAskStateReport
from omnimarket.models.model_ask_state_read import ModelAskStateReadRequest


class ModelCarryOverEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ask_id: str = Field(min_length=1)
    reference: str = Field(min_length=1, pattern=r"^[^|\r\n]+$")


class ModelCarryOverLabel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ask_id: str = Field(min_length=1)
    label: Literal["ask", "not-an-ask"]
    run_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.:/-]+$")
    engine: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.:/-]+$")
    confirmed_by: str | None = None


class ModelCarryOverPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    not_ask_label: str = Field(min_length=1)
    decision_limit: int = Field(ge=1)


class ModelCarryOverRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    read: ModelAskStateReadRequest
    ask_ids: tuple[str, ...] = Field(min_length=1)
    evidence: tuple[ModelCarryOverEvidence, ...] = ()
    labels: tuple[ModelCarryOverLabel, ...] = ()
    dry_run: bool = True


class ModelCarryOverCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int
    done: int = 0
    withdrawn: int = 0
    owned: int = 0
    parked: int = 0


class ModelCarryOverResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool
    proposed_rows: tuple[str, ...]
    report: ModelAskStateReport
    counts: ModelCarryOverCounts
    decisions: tuple[str, ...] = ()
    text: str
    applied: bool = False
