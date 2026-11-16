# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the hook-event lane join (OMN-19998)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelHookLaneJoinAgent(BaseModel):
    """One agent of the hook-event table: its span's dispatch description and its first event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    agent_id: str | None = Field(
        default=None, description="None is a session's main thread"
    )
    description: str | None = Field(
        default=None, description="The agent span's dispatch description"
    )
    first_event_at: datetime


class ModelHookLaneJoinLane(BaseModel):
    """One lane of the ledger, with the stamp of its CLAIM row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    claim_at: datetime | None = Field(
        default=None, description="None: the lane has no CLAIM in the window"
    )


class ModelHookLaneJoinRequest(BaseModel):
    """The agents, the lanes and the host of every event in the window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agents: tuple[ModelHookLaneJoinAgent, ...] = ()
    lanes: tuple[ModelHookLaneJoinLane, ...] = ()
    event_hosts: tuple[str | None, ...] = Field(
        default=(),
        description="One entry per event in the window; None is an event with no host",
    )
    name_window_seconds: int = Field(
        default=900,
        gt=0,
        description="How far either side of the lane's CLAIM its agent's first event may lie",
    )
    expected_hosts: tuple[str, ...] = Field(
        default=(),
        description="Hosts that should report; one with no event in the window is counted at zero and named",
    )
    unstamped_label: str = Field(default="unstamped", min_length=1)


class ModelHookLaneJoin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    session_id: str
    agent_id: str | None


class ModelHookLaneUnjoined(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    reason: str


class ModelHookAgentUnmatched(BaseModel):
    """An agent that fits no lane, counted and named instead of dropped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    agent_id: str | None
    description: str | None
    reason: str


class ModelHookHostCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    events: int


class ModelHookLaneJoinResult(BaseModel):
    """Lanes joined by name, lanes left unjoined with the reason, and the events per reporting host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    joins: tuple[ModelHookLaneJoin, ...] = ()
    unjoined: tuple[ModelHookLaneUnjoined, ...] = ()
    unmatched_agents: tuple[ModelHookAgentUnmatched, ...] = Field(
        default=(), description="Every agent no lane fits, with the reason"
    )
    hosts: tuple[ModelHookHostCount, ...] = Field(
        default=(), description="Most events first, then by name"
    )
    silent_hosts: tuple[str, ...] = Field(
        default=(), description="Expected hosts with no event in the window, by name"
    )
