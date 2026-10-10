# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Branch claim outcomes in canonical severity order."""

from enum import StrEnum


class EnumBranchClaimOutcome(StrEnum):
    HELD_ELSEWHERE = "held-elsewhere"
    FENCE_BEHIND = "fence-behind"
    UNIDENTIFIED = "unidentified"
    HELD_BY_PUSHER = "held-by-pusher"
    UNCLAIMED = "unclaimed"
    NO_TICKET = "no-ticket"
    DID_NOT_RUN = "did-not-run"
    NOT_APPLICABLE = "not-applicable"

    @property
    def conclusion(self) -> str | None:
        if self is self.NOT_APPLICABLE:
            return None
        if self is self.DID_NOT_RUN:
            return "failure"
        if self in {self.HELD_ELSEWHERE, self.FENCE_BEHIND, self.UNIDENTIFIED}:
            return "neutral"
        return "success"
