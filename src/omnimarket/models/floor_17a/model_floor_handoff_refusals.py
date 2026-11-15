# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's count of handed-off pull requests the landing controller refuses (OMN-23242).

A lane that hands a pull request to the landing controller (a MSG to the controller with a ``pr=`` cell) is told nothing
when the controller then defers its merge or keeps it out, so the controller writes one MSG to the hand-off lane's parent
per pull request, head and gate. The floor shows how many handed-off pull requests are deferred or kept out and how many of
the deferred ones have waited more than ``silent_after_ticks`` ticks with no such MSG on the ledger. A reading that could
not be taken is drawn unread with its reason, never as zero.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelHandoffRefusalsOverlay(_Frozen):
    """What counts: the controller's lane, the ticks after which silence is a fault, and the gates a parent cannot act on."""

    title: str = "Handed-off pull requests the controller refuses"
    # The lane the hand-off MSG row is addressed to and the parent MSG row is written from.
    controller_lane: str = Field(default="landing-controller", min_length=1)
    # A deferral counts as silent when it has lasted more than this many ticks with no parent MSG for its head and gate.
    silent_after_ticks: int = Field(default=3, ge=0)
    # How far back the ledger's hand-off and parent MSG rows are read.
    ledger_lookback_days: int = Field(default=7, gt=0)
    # The newest deferral row must be this recent for a pull request to count as deferred now: the controller writes one
    # row a tick for as long as the deferral holds, so an older last row is a deferral that ended.
    current_window_s: int = Field(default=1800, gt=0)
    # Gates the controller's own queue order explains: nothing for a parent to act on (the controller's PARENT_MSG_QUEUE_GATES).
    queue_gates: tuple[str, ...] = ("STRICT_BASE",)
    max_listed: int = Field(default=10, gt=0)


class ModelHandoffRow(_Frozen):
    """The newest hand-off MSG of one pull request: who handed it over and at which head."""

    pr: str
    head: str = ""
    lane: str


class ModelParentMsgRow(_Frozen):
    """One MSG the controller wrote to a hand-off lane's parent: the pull request, head and gate it named."""

    pr: str
    head: str
    gate: str


class ModelHandoffLedgerFacts(_Frozen):
    """What one read of the ledger gave: every hand-off and every parent MSG in the lookback."""

    handed: tuple[ModelHandoffRow, ...] = ()
    told: tuple[ModelParentMsgRow, ...] = ()


class ModelRefusalRow(_Frozen):
    """One refusal of the controller as its journals record it: a merge deferral (with its tick count) or a kept-out."""

    pr: str
    head: str
    gate: str
    kind: Literal["deferred", "kept_out"]
    ticks: int = Field(default=1, ge=1)
    newest_at: str = ""


class ModelHandoffRefusalsIn(_Frozen):
    """The cycle's reading: ``read`` with the ledger facts and the refusals, or ``unread`` with the reason."""

    status: Literal["read", "unread"]
    reason: str = ""
    ledger: ModelHandoffLedgerFacts | None = None
    refusals: tuple[ModelRefusalRow, ...] = ()


class ModelHandoffRefusalLine(_Frozen):
    pr: str
    head: str
    gate: str
    kind: Literal["deferred", "kept_out"]
    ticks: int
    handoff_lane: str
    parent_told: bool


class ModelHandoffRefusalsSection(_Frozen):
    """The board's section: counts of handed-off pull requests deferred or kept out, and of the silent deferrals."""

    status: Literal["read", "unread", "not_configured"]
    title: str = ""
    reason: str = ""
    # Handed-off pull requests deferred or kept out now (a pull request on both counts once).
    refused: int = 0
    deferred: int = 0
    kept_out: int = 0
    # Handed-off pull requests deferred more than ``silent_after_ticks`` ticks with no parent MSG for their head and gate.
    silent: int = 0
    silent_after_ticks: int = 0
    silent_prs: tuple[str, ...] = ()
    lines: tuple[ModelHandoffRefusalLine, ...] = ()
    text: str = ""
