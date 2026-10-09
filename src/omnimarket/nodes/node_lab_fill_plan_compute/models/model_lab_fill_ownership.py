# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ownership facts arrive already read so this decision never touches a source."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelLabFillOwnershipLane(BaseModel):
    """Only the identities the batch ownership verdict needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    lane: str
    ticket: str
    pr: str = ""
    repo: str = ""
    kind: str = ""


class ModelLabFillClaimRecord(BaseModel):
    """An index record distinguishes a held claim from a stale one."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    lane: str
    state: str = "held"


class ModelLabFillOpenClaim(BaseModel):
    """The caller replays CLAIM rows; supplied time decides whether they remain fresh."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    lane: str
    subject: str
    when: datetime


class ModelLabFillOwnershipRequest(BaseModel):
    """Errors are facts, never permission to interpret an unread owner as free."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    lanes: tuple[ModelLabFillOwnershipLane, ...]
    claim_index: dict[str, ModelLabFillClaimRecord] | str
    ledger_claims: tuple[ModelLabFillOpenClaim, ...] | str
    now: datetime
    staleness_hours: float = Field(ge=0, allow_inf_nan=False)
    pr_claims: dict[str, str] | str
    watcher_merged: dict[str, tuple[str, ...]] | str
    # A partial reader response must leave missing lanes ownership-unread.
    verdict_lanes: tuple[str, ...] | None = None
    returned: tuple[dict[str, object], ...] = ()


class ModelLabFillOwnershipVerdict(BaseModel):
    """Byte-identical reader detail makes disagreement reviewable."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    lane: str
    skipped: str
    detail: str


class ModelLabFillOwnershipResult(BaseModel):
    """The cleared lanes and the dispatch entries preserve selection order."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    verdicts: tuple[ModelLabFillOwnershipVerdict, ...]
    proceed: tuple[ModelLabFillOwnershipLane, ...]
    skipped: tuple[dict[str, object], ...]
    dispatched: tuple[dict[str, object], ...]
