# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The friction rollup step of the floor publish: its contract settings and the class-rule overlay (OMN-22478)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.friction_rollup import (
    ModelFrictionClassOwner,
    ModelFrictionClassRule,
)


class ModelFrictionLedgerRead(BaseModel):
    """One paged read of the ledger: the row kinds and, when the rollup needs only some of them, the term that narrows them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kinds: tuple[str, ...] = Field(min_length=1)
    term: str | None = Field(
        default=None,
        min_length=1,
        description="A case-insensitive substring a row's first line must hold; the read node narrows in the store.",
    )
    why: str = Field(min_length=1, description="What the rollup takes from these rows.")


class ModelFrictionRollupStepSettings(BaseModel):
    """What the rollup step reads and how it pages it; every value is the contract's, none is in code (OMN-23606).

    The read node refuses an answer over its byte bound, whatever the window, so the step asks for a page at a time with the
    node's own cursor. A read that ends before its last page (a failed page, ``ledger_max_pages`` reached, or the time budget
    spent) is unread: the step fails and the previous report stays, never a report drawn from the pages that did arrive.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_read_command_env: str = Field(
        min_length=1,
        description="The variable that holds the bus read command; the ledger is read through the bus, never from a file.",
    )
    ledger_page_timeout_seconds: int = Field(
        ge=1,
        le=600,
        description="The longest one page read may run; the time budget caps it further.",
    )
    ledger_page_size: int = Field(
        ge=1,
        description="Rows one page asks for, sized so a page of the longest measured rows stays under the read node's answer bound.",
    )
    ledger_max_pages: int = Field(
        ge=1,
        description="Pages one read may take; a cursor still open after this many pages is an unread read, never a short one.",
    )
    ledger_time_budget_seconds: int = Field(
        ge=1,
        description="Wall time for every page of every read together; it plus no more than one page timeout fits the step limit.",
    )
    ledger_reads: tuple[ModelFrictionLedgerRead, ...] = Field(
        min_length=1,
        description="The reads the rollup's classes are drawn from: the FRICTION rows and the STATUS rows that name a resolution.",
    )
    signals_unread_reason: str = Field(
        min_length=1,
        description="The report's signals block is drawn from every row kind, which this step does not read; it is written as unread with this reason.",
    )
    window_hours: int = Field(gt=0)
    bucket_hours: int = Field(gt=0)
    umbrella_tickets: tuple[str, ...]
    class_overlay_id: str = Field(min_length=1)
    class_overlay_file_env: str = Field(min_length=1)


class ModelFrictionClassOverlay(BaseModel):
    """The overlay home's class rules and class owners, the shape of the rollup node's ``overlay.example.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_rules: tuple[ModelFrictionClassRule, ...]
    class_owners: tuple[ModelFrictionClassOwner, ...] = ()
