# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scoped references to earlier work-ledger rows."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelLedgerHistoryScope(BaseModel):
    """Select by ticket, pull request or any touched path as of a ledger time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    pull_request: str | None = None
    touched_paths: tuple[str, ...] = ()
    as_of: datetime


class ModelLedgerHistoryReference(BaseModel):
    """Whole ledger row and its source and scope metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_ref: str
    text: str
    lane: str
    ticket: str
    pr: str
    kind: str
    outcome: str
    time: str
    blocker_class: str


class ModelLedgerHistoryResult(BaseModel):
    """A successful read can have zero matches. A failed read carries its reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    references: tuple[ModelLedgerHistoryReference, ...] = ()
    unavailable_reason: str | None = None
