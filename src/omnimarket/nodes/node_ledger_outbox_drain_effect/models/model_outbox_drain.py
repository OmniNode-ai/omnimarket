# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for draining a host's bus outbox."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelOutboxDrainRequest(BaseModel):
    """Replay the queued requests of one ledger's bus outbox, or with ``refused`` the ones in ``refused/``.

    ``limit`` bounds one run (the next accepted append continues it). ``publisher_argv`` (with ``host`` and the
    ``lane`` used for a request whose rows name none) is the publisher command the default sender runs; a handler built
    with a sender ignores it. ``now`` fixes the clock for a test or a replay; unset, the handler reads its clock at each
    restamp.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_path: str
    refused: bool = False
    limit: int = Field(default=50, ge=1)
    now: datetime | None = None
    publisher_argv: tuple[str, ...] = ()
    lane: str = "outbox-drain"
    host: str = ""


class ModelOutboxDrainResult(BaseModel):
    """Counts of one drain run; ``remaining`` counts the files the drained directory still holds; ``stopped`` names why
    the run stopped early, or is empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    replayed: int = 0
    refused: int = 0
    remaining: int = 0
    stopped: str = ""
