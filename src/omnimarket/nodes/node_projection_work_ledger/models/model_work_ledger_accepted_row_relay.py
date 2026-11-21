# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed boundaries of the accepted-row bootstrap relay and the old-topic republish command (OMN-25630, L12)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_work_ledger_accepted_row import ModelAcceptedRowEvent


class ModelAcceptedRowRelayRequest(BaseModel):
    """One relay pass: the history to bootstrap, the watermark H, and the live events offered now.

    ``history`` is every row the table holds at the start of the bootstrap (legacy rows without a
    sequence, then sequenced rows); ``watermark`` is the highest ``ledger_seq`` among them at that
    moment. ``live`` is what the outbox holds: events judged after the watermark.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    history: tuple[ModelAcceptedRowEvent, ...] = ()
    watermark: int = Field(ge=0)
    live: tuple[ModelAcceptedRowEvent, ...] = ()


class ModelAcceptedRowRelayResult(BaseModel):
    """What the pass put on the partition and what it still holds back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bootstrap_published: int = Field(ge=0)
    bootstrap_already_on_topic: int = Field(ge=0)
    bootstrap_complete: bool
    live_published: int = Field(ge=0)
    live_duplicates_dropped: int = Field(ge=0)
    live_held_row_ids: tuple[str, ...] = ()
    above_watermark_history_skipped: int = Field(default=0, ge=0)


class ModelRepublishRequest(BaseModel):
    """The accepted-row topic read back, in offset order, to be written to the old row topics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: tuple[ModelAcceptedRowEvent, ...]


class ModelRepublishedRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    row_id: str


class ModelRepublishResult(BaseModel):
    """Each distinct row went to the one old topic of its row type and version, once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    published: tuple[ModelRepublishedRow, ...]
    duplicates_dropped: int = Field(ge=0)
    already_on_old_topics: int = Field(default=0, ge=0)


__all__: list[str] = [
    "ModelAcceptedRowRelayRequest",
    "ModelAcceptedRowRelayResult",
    "ModelRepublishRequest",
    "ModelRepublishResult",
    "ModelRepublishedRow",
]
