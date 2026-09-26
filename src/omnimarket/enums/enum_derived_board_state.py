# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The board state a ticket's facts entail, independent of what the board says.

Every member corresponds to one edge of the OMN-16729 state machine. The
derivation is a fact, not a judgement: each member names the fact pattern that
entails it, and a ticket whose facts entail nothing coherent lands on
``AMBIGUOUS`` rather than on a guess.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16731: node_board_truth_compute
"""

from __future__ import annotations

from enum import StrEnum


class EnumDerivedBoardState(StrEnum):
    """State entailed by a ticket's facts.

    IN_REVIEW: at least one open, non-draft PR cites the ticket — real review
        state, as opposed to the state PR-merge automation fabricates
        (OMN-16536).
    IN_PROGRESS: a live claim exists — an unterminated ledger CLAIM row, branch
        commits inside the staleness window, or an open draft PR.
    BACKLOG: the reaper edge. The claim went stale: no unterminated claim, no
        open or merged PR, and no commits inside the window.
    DEFER_TO_DOD_VERIFY: the facts point at completion. This node never flips a
        ticket Done — that edge belongs to OMN-16106's dod_verify path — so this
        member is always reported and never acted on.
    AMBIGUOUS: the facts entail more than one state with no declared precedence,
        or a load-bearing fact is unknown. Fails closed to no flip.
    """

    IN_REVIEW = "in_review"
    IN_PROGRESS = "in_progress"
    BACKLOG = "backlog"
    DEFER_TO_DOD_VERIFY = "defer_to_dod_verify"
    AMBIGUOUS = "ambiguous"


__all__ = [
    "EnumDerivedBoardState",
]
