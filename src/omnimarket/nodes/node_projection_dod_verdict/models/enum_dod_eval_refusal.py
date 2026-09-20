# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a definition-of-done verification did not count as done (OMN-18900).

The refusal is TYPED because the caller acts on it differently per member: a
run refused for having failed checks is work to redo, and a run refused for
proving no behaviour is a CONTRACT to fix -- the evidence items bind nothing
that executes. Collapsing both into a boolean false, which is what reading
``status == verified`` alone does today, hides the second case entirely.

It is not hypothetical. In the one verify payload the 2026-09-20 inventory
could read, the run reported 88 checks of which 72 were non-probative and
ZERO were behaviour-proving. Under a status-only reading a payload of that
shape is indistinguishable from one whose checks executed the product.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDodEvalRefusal(StrEnum):
    """The reason one verification is not counted as done."""

    # At least one evidence check FAILED. Checked before the status, because
    # the counts are the evidence and the status is a summary of them: a
    # verdict whose status disagrees with its own failure count is a defect
    # in the summariser, and the honest reading is the count.
    CHECKS_FAILED = "checks_failed"

    # The run reached a terminal status other than verified -- failed,
    # skipped, unresolved, or the model default pending, which reads as "not
    # yet attempted" and is never an outcome.
    STATUS_NOT_VERIFIED = "status_not_verified"

    # Zero verdict-bearing checks ran. A verdict over no checks is green by
    # vacuum. It is a separate member from the one below because the remedy
    # differs: here the contract declares no evidence at all, there it
    # declares evidence that proves nothing.
    NO_CHECKS_RUN = "no_checks_run"

    # Every check passed and none of them executed the claimed behaviour.
    # This is the conjunct the plan added and the one the existing surfaces
    # do not carry: a definition of done that thins out silently makes the
    # attempts-until-done metric fall without any work getting better, which
    # is the most serious risk that plan records.
    NO_BEHAVIOR_PROVING_CHECK = "no_behavior_proving_check"


__all__ = ["EnumDodEvalRefusal"]
