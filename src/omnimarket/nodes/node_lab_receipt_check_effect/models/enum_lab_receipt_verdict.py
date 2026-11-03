# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the lab receipt check concluded about a pull request head (OMN-21826)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumLabReceiptVerdict(StrEnum):
    """The verdict of one check.

    ``NOT_REQUIRED`` and ``PASS`` let the merge go. ``REFUSED`` (the validator reported an error
    issue) and ``UNREADABLE`` (a fact could not be read) block it: a fact that could not be read is
    never a fact that held.
    """

    NOT_REQUIRED = "not_required"
    PASS = "pass"
    REFUSED = "refused"
    UNREADABLE = "unreadable"

    @property
    def blocks_merge(self) -> bool:
        """True for the two verdicts that stop the merge."""
        return self in (EnumLabReceiptVerdict.REFUSED, EnumLabReceiptVerdict.UNREADABLE)


__all__ = ["EnumLabReceiptVerdict"]
