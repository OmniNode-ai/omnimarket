# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""adapter_pr_collector — derives PR status from friction events.

Reads friction events with PR-related friction_types and categorizes them
into merged, open, and failed PR URL lists.
"""

from __future__ import annotations

from omnimarket.nodes.node_session_post_mortem.models.model_post_mortem_report import (
    ModelFrictionEvent,
)

# Friction types that indicate PR state
_PR_MERGED_TYPES: frozenset[str] = frozenset({"pr_merged"})
_PR_OPEN_TYPES: frozenset[str] = frozenset({"pr_open", "pr_review_pending"})
_PR_FAILED_TYPES: frozenset[str] = frozenset({"pr_merge_failed", "pr_ci_failed"})


def collect_pr_status(
    friction_events: list[ModelFrictionEvent],
) -> tuple[list[str], list[str], list[str]]:
    """Derive PR status lists from friction events.

    Args:
        friction_events: All friction events from the session.

    Returns:
        Tuple of (prs_merged, prs_open, prs_failed) — lists of PR URLs/IDs
        extracted from friction event descriptions.
    """
    prs_merged: list[str] = []
    prs_open: list[str] = []
    prs_failed: list[str] = []

    for event in friction_events:
        if event.friction_type in _PR_MERGED_TYPES:
            prs_merged.append(event.description)
        elif event.friction_type in _PR_OPEN_TYPES:
            prs_open.append(event.description)
        elif event.friction_type in _PR_FAILED_TYPES:
            prs_failed.append(event.description)

    return prs_merged, prs_open, prs_failed


__all__: list[str] = ["collect_pr_status"]
