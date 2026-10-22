# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-host lane activity and hook-event recency in, the quiet-host verdict out (OMN-18603)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelHostActivity(BaseModel):
    """What the ledger and the hook-event table each say about one pool host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The pool host id the ledger's ``host=`` cell uses: h101, h201, h202.
    host: str
    # The stamps of the ledger rows that put a lane on this host, oldest first.
    lane_stamps: tuple[AwareDatetime, ...] = ()
    # The newest hook event the host sent; None when it sent none inside the read window.
    last_event_at: AwareDatetime | None = None


class ModelQuietHostRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    hosts: tuple[ModelHostActivity, ...] = ()
    # False when the hook-event table could not be read: every host is then UNKNOWN, never LIVE.
    events_readable: bool = True
    lane_window_minutes: int = Field(default=60, gt=0)
    quiet_minutes: int = Field(default=30, gt=0)


class ModelQuietHostResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    alarm: bool
    state: Literal["LIVE", "QUIET", "UNKNOWN"]
    quiet_hosts: tuple[str, ...] = ()
    # One line per quiet host: its last event and how long lanes have run since.
    findings: tuple[str, ...] = ()
    fix: str = ""
    friction_cells: str = ""
    alert_text: str = ""
