# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Standardized failure category strings for cross-run failure history (OMN-23854)."""

from __future__ import annotations

from enum import StrEnum


class EnumFailureCategory(StrEnum):
    """Standardized failure category strings for cross-run failure history."""

    CI_TEST = "ci_test"
    CI_LINT = "ci_lint"
    CI_GATE = "ci_gate"
    PR_TITLE = "pr_title"
    CONFLICT = "conflict"
    CHANGES_REQUESTED = "changes_requested"
    THREADS_BLOCKED = "threads_blocked"
    BRANCH_STALE = "branch_stale"
    SCAN_FAILED = "scan_failed"
    POLISH_FAILED = "polish_failed"
    NEEDS_HUMAN = "needs_human"


__all__ = ["EnumFailureCategory"]
