# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts and result of the lane-resend read (OMN-17427, moved here by OMN-23250)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelLaneResendRequest:
    """Ledger rows and the clock the resend backlog is read at."""

    now: str
    ledger_lines: tuple[str, ...]
    parent_ticket: str
    window_hours: int = 48
    limit: int = 12


@dataclass(frozen=True, slots=True)
class ModelLaneResendCandidate:
    """A ticket whose newest lane TERMINAL in the window did not finish the work."""

    ticket: str
    lane: str
    outcome: str
    ended_at: str
    blocker: str
    evidence: str


@dataclass(frozen=True, slots=True)
class ModelLaneResendResult:
    """Resend candidates, newest first, and the count of lanes dropped per reason."""

    candidates: tuple[ModelLaneResendCandidate, ...]
    dropped: tuple[tuple[str, int], ...]
