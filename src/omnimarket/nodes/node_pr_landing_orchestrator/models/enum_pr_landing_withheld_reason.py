# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why the arm decision withheld a green head on its ledger facts (OMN-20866)."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingWithheldReason(StrEnum):
    """One code per gate fact that stops an arm; the transition names it."""

    # A HOLD row in force names the PR or its repository.
    LEDGER_HOLD_IN_FORCE = "ledger_hold_in_force"
    # The ledger projection could not be read, or has not moved within its bound.
    LEDGER_HOLDS_UNKNOWN = "ledger_holds_unknown"
    # A lab-proof repository's head has no PASS lab proof.
    LAB_PASS_MISSING = "lab_pass_missing"
    # The lab proof projection could not be read.
    LAB_PASS_UNKNOWN = "lab_pass_unknown"


__all__: list[str] = ["EnumPrLandingWithheldReason"]
