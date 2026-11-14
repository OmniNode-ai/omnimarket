# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cause-lane dispatch, skip and refusal shapes shared by the stall and alarm-owner responders."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelCauseDispatch(BaseModel):
    """One cause lane to start: the cause key, its class, and the first stall row that named it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cause_key: str
    cause_class: str
    subject: str
    lane: str
    ticket: str
    first_stamp: str
    occurrences: int


class ModelCauseSkip(BaseModel):
    """A cause key that starts nothing, with the reason: already-dispatched, covered, tick-owned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cause_key: str
    reason: str
    occurrences: int


class ModelRowRefusal(BaseModel):
    """A stall row the responder refused to read, with the row's stamp and the typed reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stamp: str
    reason: str
