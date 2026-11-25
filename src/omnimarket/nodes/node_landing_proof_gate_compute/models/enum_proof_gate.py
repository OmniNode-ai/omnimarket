# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The answers and reason codes of the landing proof gate."""

from __future__ import annotations

from enum import StrEnum


class EnumProofAnswer(StrEnum):
    """PROVEN: the pull request may merge. DRAFT: it must be in draft, with the reasons named."""

    PROVEN = "PROVEN"
    DRAFT = "DRAFT"


class EnumProofCode(StrEnum):
    PROOF_UNREADABLE = "proof_unreadable"
    LAB_LINE_MISSING = "lab_line_missing"
    LAB_PROOF_OTHER_CHANGE = "lab_proof_other_change"
    KNOWN_GOOD_MISSING = "known_good_missing"
    KNOWN_BAD_MISSING = "known_bad_missing"
    DO_NOT_LAND = "do_not_land"
