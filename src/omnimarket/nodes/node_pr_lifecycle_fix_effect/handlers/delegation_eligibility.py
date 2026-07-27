# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation eligibility gate for handler_pr_lifecycle_fix (WS-D/D2, OMN-13940).

Decides whether a CODE_FAILURE / CHANGES_REQUESTED fix may be routed to a
delegated (non-Claude) fix path instead of the agent-dispatch path. This is a
pure, side-effect-free function — the two-strike counter is read by the
caller and passed in as ``strikes`` so this module has no I/O.

Refusal is the default: any denylist hit, unknown blast radius, unsupported
block_reason, or a tripped two-strike counter is ineligible. RECEIPT_FAILURE
is never eligible — it stays on the agent path unconditionally per safety
bar #5 and is not even passed through ``is_delegation_eligible``; callers
must route it before reaching this check.

Takes primitive args rather than ``ModelPrLifecycleFixCommand`` so
``node_pr_delegated_fix_effect`` (which re-checks eligibility against the
ACTUAL diff it produced, defense-in-depth) never needs a cross-node model
reach-in into this node — see ``tests/test_no_cross_node_reach_in.py``.
"""

from __future__ import annotations

# Blast-radius cap: delegation is only considered for small, contained diffs.
# These are deliberately conservative for Slice 0 (deterministic ruff-only) —
# widen only with acceptance-rate evidence per the critique's ladder gating.
MAX_DELEGATION_FILES = 3
MAX_DELEGATION_LINES = 60

# Two-strike permanent-escalation threshold (safety bar #7).
TWO_STRIKE_THRESHOLD = 2

# Path-substring denylist (case-insensitive). Any changed_files hit refuses.
_DENYLIST_PATH_PATTERNS: tuple[str, ...] = (
    "onex_change_control/",
    "deploy-gate",
    "no-raw-prod-bypass",
    "prod_promotion_grants",
    "/auth/",
    "auth_",
    "_auth",
)

# Content keyword denylist (case-insensitive substring match against changed
# file paths and review_context_text). Any hit refuses.
_DENYLIST_KEYWORDS: tuple[str, ...] = (
    "security",
    "auth",
    "crypto",
    "injection",
    "secret",
    "password",
    "credential",
    "token",
)

# String values, not the EnumPrBlockReason import, so this module stays
# import-free of any node's models package (both this node's own and any
# sibling node's) — StrEnum members compare equal to their string value, so
# callers may pass either the enum or a plain string.
_DELEGATION_ELIGIBLE_REASONS: frozenset[str] = frozenset(
    {"code_failure", "changes_requested"}
)


def is_delegation_eligible(
    *,
    block_reason: str,
    changed_files: list[str],
    diff_total_lines: int,
    review_context_text: str = "",
    strikes: int,
) -> tuple[bool, str]:
    """Return ``(eligible, reason)``.

    ``reason`` is always populated (both on refusal and on approval) so
    callers can log/record why a routing decision was made.
    """
    if block_reason not in _DELEGATION_ELIGIBLE_REASONS:
        return False, f"block_reason_not_eligible:{block_reason}"

    if strikes >= TWO_STRIKE_THRESHOLD:
        return False, "two_strike_permanent_escalation"

    if not changed_files:
        return False, "changed_files_unknown"

    if len(changed_files) > MAX_DELEGATION_FILES:
        return False, "blast_radius_too_many_files"

    if diff_total_lines > MAX_DELEGATION_LINES:
        return False, "blast_radius_too_many_lines"

    lowered_paths = [path.lower() for path in changed_files]
    for pattern in _DENYLIST_PATH_PATTERNS:
        if any(pattern in path for path in lowered_paths):
            return False, f"denylisted_path:{pattern}"

    haystack = " ".join(lowered_paths) + " " + review_context_text.lower()
    for keyword in _DENYLIST_KEYWORDS:
        if keyword in haystack:
            return False, f"denylisted_keyword:{keyword}"

    return True, "eligible"


__all__: list[str] = [
    "MAX_DELEGATION_FILES",
    "MAX_DELEGATION_LINES",
    "TWO_STRIKE_THRESHOLD",
    "is_delegation_eligible",
]
