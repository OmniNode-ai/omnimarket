# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hostile-reviewer phase enum (OMN-13210 / B1).

Preserves the EnumHostileReviewerPhase value vocabulary from the deleted
node_hostile_reviewer shell so the ModelHostileReviewerCompletedEvent output
shape (and its ``final_phase`` field) is byte-stable for replay. The rebuilt
orchestrator coordinates over the bus and records terminal phase only — there
is no in-process FSM ``advance()``.
"""

from __future__ import annotations

from enum import StrEnum


class EnumHostileReviewerPhase(StrEnum):
    """Terminal-phase vocabulary recorded by the hostile-reviewer orchestrator.

    The value vocabulary is preserved verbatim from the legacy shell
    (INIT -> DISPATCH_REVIEWS -> AGGREGATE -> CONVERGENCE_CHECK -> REPORT ->
    DONE, plus FAILED) so the completed event's ``final_phase`` value stays
    byte-stable for replay. The rebuilt orchestrator records DONE or FAILED;
    CONVERGENCE_CHECK is retained in the vocabulary for output-shape stability.
    """

    INIT = "init"
    DISPATCH_REVIEWS = "dispatch_reviews"
    AGGREGATE = "aggregate"
    CONVERGENCE_CHECK = "convergence_check"
    REPORT = "report"
    DONE = "done"
    FAILED = "failed"


__all__: list[str] = ["EnumHostileReviewerPhase"]
