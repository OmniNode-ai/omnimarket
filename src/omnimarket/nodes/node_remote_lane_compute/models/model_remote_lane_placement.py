# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the remote-lane placement decision (OMN-20669).

A host reading arrives already evaluated: the admission bar, the Codex bar, the lane
slots and the free capacity are the placement reader's numbers for that host at read
time. This node chooses among them; it reads no host and holds no host identity.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")

CODEX_ENGINE = "codex"


class ModelRemoteLaneHostReading(BaseModel):
    """One pool host as the placement reader saw it."""

    model_config = _FROZEN

    name: str = Field(min_length=1)
    local: bool = False
    engines: tuple[str, ...] = ()
    lane_admission_refusal: str | None = None
    codex_refusal: str | None = None
    lane_slots: int = 0
    lane_cap: int = 0
    placed: int = Field(default=0, ge=0)
    rank_free: float = 0.0
    mem_avail_gb: float = 0.0


class ModelRemoteLanePlacementRequest(BaseModel):
    """The lane's engine, an optional pinned host, the live host marks and every reading."""

    model_config = _FROZEN

    engine: str = Field(min_length=1)
    pinned_host: str | None = None
    limited_hosts: tuple[str, ...] = ()
    auth_expired_hosts: tuple[str, ...] = ()
    readings: tuple[ModelRemoteLaneHostReading, ...] = ()


class ModelRemoteLaneHostVerdict(BaseModel):
    """Why one host did or did not take the lane."""

    model_config = _FROZEN

    host: str
    reason: str


class ModelRemoteLanePlacementResult(BaseModel):
    """The chosen host and engine, or none, with a verdict for every reading."""

    model_config = _FROZEN

    host: str | None
    engine: str
    local: bool = False
    verdicts: tuple[ModelRemoteLaneHostVerdict, ...] = ()
