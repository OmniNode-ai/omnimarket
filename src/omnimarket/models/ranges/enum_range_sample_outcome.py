# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outcome of one sampled case in one range run."""

from __future__ import annotations

from enum import StrEnum


class EnumRangeSampleOutcome(StrEnum):
    """PASS or FAIL against the case's own criterion, or INCOMPLETE.

    INCOMPLETE is a sample that produced no judgeable outcome: the run was
    cancelled, the delegation never completed, the score is null. It is kept
    distinct so it can be reported by count, and it counts as a failure in the
    pass rate; it is never scored zero in a distribution.
    """

    PASS = "pass"
    FAIL = "fail"
    INCOMPLETE = "incomplete"
