# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, verdicts and overlay of node_ask_done_check_effect."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelLedgerAppendRoute,
)

# A done check names what it reads: ``floor:piece=6,status=working`` reads the floor, and
# ``row:lane=host-reading,probe=ledger-durability,verdict=WORKING`` reads the ledger for a row
# with those cells stamped after the ask. No space and no pipe, so it is one ledger cell.
_SPEC = r"^(floor|row):[A-Za-z0-9_.-]+=[A-Za-z0-9_.:-]+(,[A-Za-z0-9_.-]+=[A-Za-z0-9_.:-]+)*$"


class EnumCheckVerdict(StrEnum):
    """TRUE closes the ask; FALSE keeps it open; UNKNOWN is a check that could not read."""

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class ModelAskToCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ask_id: str = Field(pattern=r"^ask-[0-9a-f]{10}$")
    said_at: AwareDatetime
    done_check: str = Field(pattern=_SPEC)


class ModelAskDoneCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asks: tuple[ModelAskToCheck, ...] = Field(min_length=1)
    write: bool


class ModelAskCheckVerdict(BaseModel):
    """One ask's done-check verdict and the ledger row that records it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ask_id: str
    done_check: str
    verdict: EnumCheckVerdict
    reason: str
    row: str
    written: Literal["yes", "no", "failed"]


class ModelAskDoneCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: tuple[ModelAskCheckVerdict, ...]


class ModelFloorRoute(BaseModel):
    """The floor's JSON: the URL is deployment data, read from the variable this names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url_env: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1, le=120)
    max_age_seconds: int = Field(ge=1)


class ModelAskDoneCheckOverlay(BaseModel):
    """Every deployment fact; no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    floor: ModelFloorRoute
    read: ModelCommandLedgerReadRoute
    append: ModelLedgerAppendRoute
