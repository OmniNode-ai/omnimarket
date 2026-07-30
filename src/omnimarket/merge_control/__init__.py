# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge-control shared classifiers (OMN-14765, epic OMN-14643).

Shared, node-agnostic classifiers consumed by the PR-lifecycle nodes
(``node_pr_lifecycle_inventory_compute`` / ``node_pr_lifecycle_triage_compute``
/ ``node_pr_lifecycle_orchestrator``) and the ``merge_sweep`` skill. Promoting
these out of any single node keeps the "no node imports another node's private
package" boundary (see ``omnimarket/CLAUDE.md``) intact.
"""

from __future__ import annotations

from omnimarket.merge_control.hold_marker import (
    DO_NOT_MERGE_RE,
    HOLD_MARKER_RE,
    EnumMergeHoldStatus,
    MergeHoldDecision,
    evaluate_merge_hold,
    match_hold_token,
)
from omnimarket.merge_control.outage_circuit_breaker import (
    EnumOutageBreakerState,
    OutageCircuitBreaker,
)
from omnimarket.merge_control.reason_code_classifier import (
    EnumMergeCheckReasonCode,
    MergeCheckFacts,
    classify,
    classify_dict,
    classify_job,
    dominant_reason_code,
    facts_from_job,
)

__all__: list[str] = [
    "DO_NOT_MERGE_RE",
    "HOLD_MARKER_RE",
    "EnumMergeCheckReasonCode",
    "EnumMergeHoldStatus",
    "EnumOutageBreakerState",
    "MergeCheckFacts",
    "MergeHoldDecision",
    "OutageCircuitBreaker",
    "classify",
    "classify_dict",
    "classify_job",
    "dominant_reason_code",
    "evaluate_merge_hold",
    "facts_from_job",
    "match_hold_token",
]
