# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the running-read due row (OMN-26491)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest


class ModelMergedPrFact(BaseModel):
    """One merged pull request, as the landing controller read it at merge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$")
    ticket: str | None = Field(default=None, pattern=r"^OMN-[0-9]+$")
    owner: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{0,62}$",
        description="The ticket owner the alarm is addressed to.",
    )
    merged_at: AwareDatetime
    runtime_affecting: bool
    pr_body: str = Field(
        default="",
        description="The merged pull request's body, which names the reading.",
    )


class ModelRunningReadDueRequest(BaseModel):
    """``write_due_row`` maps one merge to its due row. ``read_due_rows`` reads the ledger text at ``now``:
    the open due rows, the alarms owed, and the tickets held out of Done."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["write_due_row", "read_due_rows"]
    merged: ModelMergedPrFact | None = None
    ledger_text: str = ""
    now: AwareDatetime | None = None


class ModelRunningReadAlarm(BaseModel):
    """One alarm owed to a ticket owner for one due row with no run line by its due time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    due_id: str
    ticket: str
    owner: str
    append_request: ModelWorkLedgerAppendRequest


class ModelRunningReadDueResult(BaseModel):
    """What the node answers for either operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["write_due_row", "read_due_rows"]
    due_id: str | None = None
    append_request: ModelWorkLedgerAppendRequest | None = None
    refusal: str | None = None
    open_due_ids: list[str] = Field(default_factory=list)
    alarms: list[ModelRunningReadAlarm] = Field(default_factory=list)
    blocked_tickets: list[str] = Field(default_factory=list)
    blocked_reason: str = ""
