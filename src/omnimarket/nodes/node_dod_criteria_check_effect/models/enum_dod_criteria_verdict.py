# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the trusted criteria check concluded about a pull request head (OMN-21676)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDodCriteriaVerdict(StrEnum):
    """The verdict of one ticket, or of the whole pull request (the worst of its tickets).

    ``PASS`` and ``NOT_ENGAGED`` let the merge go. ``DRIFT`` (the live criteria are not the
    ones the contract was accepted against) and ``UNREADABLE`` (the pull request, its
    contract or its ticket could not be read) block it: an unreadable ticket is never an
    unchanged one.
    """

    NOT_ENGAGED = "not_engaged"
    PASS = "pass"
    DRIFT = "drift"
    UNREADABLE = "unreadable"

    @property
    def blocks_merge(self) -> bool:
        """True for the two verdicts that stop the merge."""
        return self in (EnumDodCriteriaVerdict.DRIFT, EnumDodCriteriaVerdict.UNREADABLE)

    @property
    def severity(self) -> int:
        """Order of the verdicts, worst highest, for taking the worst of several."""
        return _SEVERITY[self]


_SEVERITY: dict[EnumDodCriteriaVerdict, int] = {
    EnumDodCriteriaVerdict.NOT_ENGAGED: 0,
    EnumDodCriteriaVerdict.PASS: 1,
    EnumDodCriteriaVerdict.DRIFT: 2,
    EnumDodCriteriaVerdict.UNREADABLE: 3,
}

__all__ = ["EnumDodCriteriaVerdict"]
