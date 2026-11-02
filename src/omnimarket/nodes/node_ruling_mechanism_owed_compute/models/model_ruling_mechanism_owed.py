# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger rows and a tick time in, owed rows out.

The caller reads the ledger and its archive rolls; this node reads nothing.
The tick time is a request field so the same rows and the same tick always give
the same answer.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_ruling_mechanism_owed_request import (
    ModelRulingMechanismOwedRequest as ModelRulingMechanismOwedRequest,
)


class ModelOwedRow(BaseModel):
    """One mechanism ticket whose lane ended without finishing and was never picked up."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    ticket: str
    terminal_at: str
    lane: str
    outcome: str
    blocker: str = ""
    rulings: tuple[str, ...]


class ModelRulingMechanismOwedResult(BaseModel):
    """Owed rows, plus the keys of terminals that are not yet one tick old."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick_at: str
    owed: tuple[ModelOwedRow, ...]
    awaiting_tick: tuple[str, ...] = ()
    mechanism_ticket_count: int = 0
