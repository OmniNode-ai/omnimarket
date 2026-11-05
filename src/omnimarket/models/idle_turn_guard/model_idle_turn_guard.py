# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed facts and decision of the idle-turn guard (OMN-19998).

The Stop hook's adapter supplies ``ModelIdleTurnGuardFacts`` beside the Stop payload: the hold and stop rows in
force, the hosts that refuse lanes, and the runner receipts it read. The pure rule decides from those facts and the
finding lines the turn read. Shared here so the Stop guard of ``node_idle_slot_fill_effect`` and the
``node_idle_turn_guard_compute`` node use one rule without importing each other.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EnumIdleTurnGuardVerdict(StrEnum):
    PASS = "pass"  # nothing to answer, or the turn dispatched
    HELD = "held"  # a hold or stop row in force holds the guard
    REFUSE = "refuse"  # the turn read free headroom or a red and dispatched nothing


class EnumIdleTurnGuardHoldKind(StrEnum):
    HOLD = "hold"
    STOP = "stop"


class ModelIdleTurnGuardHold(BaseModel):
    """One hold or stop row the adapter read as in force (no RELEASE, not expired)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    kind: EnumIdleTurnGuardHoldKind
    stamp: AwareDatetime
    scope: str = ""
    text: str = ""


class ModelRunnerReceipt(BaseModel):
    """One remote-lane runner receipt: the lane, its placement state and when the runner wrote it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    state: str = Field(min_length=1)
    at: AwareDatetime
    host: str = ""


class ModelIdleTurnGuardFacts(BaseModel):
    """What the adapter read for one Stop turn. Every field defaults empty, which is the guard's earlier behaviour."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start: AwareDatetime | None = None
    holds_in_force: tuple[ModelIdleTurnGuardHold, ...] = ()
    lane_refusing_hosts: tuple[str, ...] = ()
    runner_receipts: tuple[ModelRunnerReceipt, ...] = ()


class ModelIdleTurnGuardPolicy(BaseModel):
    """The rule's data, from the node contract's ``config.idle_turn_guard`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatch_receipt_states: tuple[str, ...] = Field(min_length=1)


class ModelIdleTurnGuardDecisionRequest(BaseModel):
    """The finding lines the turn read (already sorted by kind), whether a tool call dispatched, and the facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    headroom_lines: tuple[str, ...] = ()
    unowned_lines: tuple[str, ...] = ()
    broken_lines: tuple[str, ...] = ()
    tool_dispatched: bool = False
    facts: ModelIdleTurnGuardFacts = ModelIdleTurnGuardFacts()
    now: AwareDatetime


class ModelIdleTurnGuardDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumIdleTurnGuardVerdict
    reason: str = ""
    shown: tuple[str, ...] = ()
    headroom_count: int = 0
    unowned_count: int = 0
    broken_count: int = 0
    # Hosts whose headroom lines were dropped because they refuse lanes.
    discounted_hosts: tuple[str, ...] = ()
    # Lanes whose runner receipt in the window counted as this turn's dispatch.
    receipt_lanes: tuple[str, ...] = ()
    hold_ids: tuple[str, ...] = ()
