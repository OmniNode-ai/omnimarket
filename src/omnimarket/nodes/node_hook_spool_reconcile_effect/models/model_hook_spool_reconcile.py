# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the hook spool reconcile node (OMN-19998): the command, the spool reading, the decision and the result."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelHookSpoolReconcileCommand(BaseModel):
    """Whether to replay the eligible dead letters back into the journal dir."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    replay: bool


class ModelSpoolRecord(BaseModel):
    """One dead-lettered record: its name, the dir it sits in, its event class and its age."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    location: str
    event_type: str
    age_seconds: int = 0


class ModelHookSpoolDecision(BaseModel):
    """The partition of the dead letters: the replay candidates and the held, and the unalarmed losses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    replay: tuple[ModelSpoolRecord, ...]
    held_refused: int
    held_young: int
    unalarmed_losses: int


class ModelHookSpoolReading(BaseModel):
    """What the spool held on this run: the pending, the dead letters, the losses and the drainer's refused classes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pending: int
    dead_lettered: int
    unreadable: int
    by_class: dict[str, int]
    losses_total: int
    losses_alarmed: int
    refused_event_types: tuple[str, ...]


class ModelHookSpoolReconcileResult(BaseModel):
    """What the run read, replayed, held and alarmed, and how it ended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    status: Literal["drained", "draining", "stuck", "unread"]
    before: ModelHookSpoolReading
    after: ModelHookSpoolReading
    replayed: int
    held_refused: int
    held_young: int
    alarmed_losses: int
    reason: str = ""
