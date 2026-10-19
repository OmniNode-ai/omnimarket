# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the lab Claude token effect did. Every field is a reference, a name, a path
or a verdict: no token value is ever a field of a result or an event."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

EnumLabClaudeTokenOutcome = Literal[
    # set: the token answered the probe and is live on the host.
    "deployed",
    # set / rotate: the token did not answer; the host's previous token is back.
    "restored",
    # add_spare: the token is in the pool.
    "spare_added",
    # rotate_limited: the host now runs on a spare account.
    "rotated",
    # rotate_limited: no spare with headroom; ``reported`` says whether this is the first time.
    "no_spare",
    # rotate_limited: the host carries no ``.limited`` marker, so nothing was done.
    "not_limited",
    # dry run: the plan only.
    "planned",
]


class ModelLabClaudeTokenRotatedEvent(BaseModel):
    """A host moved from one account to another, automatically, after a usage limit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    from_account: str | None
    to_account: str
    to_secret_ref: str
    limited_until: str | None
    rotated_at: datetime


class ModelLabClaudeTokenResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["set", "add_spare", "rotate_limited"]
    outcome: EnumLabClaudeTokenOutcome
    host: str | None = None
    account: str | None = None
    secret_ref: str | None = None
    # Whether the probe answered (None: no probe ran).
    probe_answered: bool | None = None
    marker_cleared: bool = False
    # ``no_spare`` only: False when this host's no-spare state was already reported.
    reported: bool = True
    events: tuple[ModelLabClaudeTokenRotatedEvent, ...] = ()
    # Operator-facing lines: references and verdicts only.
    lines: tuple[str, ...] = ()
