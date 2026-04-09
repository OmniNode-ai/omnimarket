# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumPrTriageCategory — triage classification categories for PRs."""

from __future__ import annotations

from enum import StrEnum


class EnumPrTriageCategory(StrEnum):
    """Triage classification categories for a PR.

    - GREEN: CI passing, approved, no conflicts — ready to merge.
    - RED: CI failing or errored — needs fix before merge.
    - CONFLICTED: Has merge conflicts — needs rebase.
    - NEEDS_REVIEW: CI passing (or pending) but lacks required approval.
    """

    GREEN = "green"
    RED = "red"
    CONFLICTED = "conflicted"
    NEEDS_REVIEW = "needs_review"


__all__: list[str] = ["EnumPrTriageCategory"]
