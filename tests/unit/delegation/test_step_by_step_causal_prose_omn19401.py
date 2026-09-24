# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19401: step_by_step_explanation must accept causal-connective prose.

Reproduces the live veto on correlation_id
174ea493-c4b8-4174-aac4-b158d439b424 (delegation_events, 2026-09-23T18:45:34Z):
a factually-correct answer to "Explain why a hash map lookup is on average
constant time, and when it degrades, in under 120 words." was vetoed 3/3
local retries by ``step_by_step_explanation`` because it walked the mechanism
with "Consequently"/"However"/"Additionally" rather than the literal tokens
"step"/"1."/"first"/"then".
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _HEURISTIC_CONTAINS_ANY_CHECKS,
    _check_contains_any,
)

# Verbatim response text from correlation_id 174ea493-c4b8-4174-aac4-b158d439b424.
_HASH_MAP_RESPONSE = (
    "A hash map achieves average O(1) lookup by using a hash function to map "
    "keys directly to array indices. This distributes elements uniformly "
    "across buckets, ensuring minimal collisions. Consequently, retrieving "
    "an item requires only a single hash calculation and array access, "
    "independent of the total dataset size.\n\n"
    "However, performance degrades to O(n) when collisions become frequent. "
    'This occurs if the hash function is poor, creating "hot spots" where '
    "many keys map to the same index. Additionally, as the map fills, the "
    "load factor increases, causing buckets to overflow into linked lists "
    "or trees. If the table isn't resized to maintain a low load factor, "
    "these long chains force linear scans, significantly slowing down "
    "operations."
)


def _run_step_by_step_check(content: str) -> str | None:
    category, markers = _HEURISTIC_CONTAINS_ANY_CHECKS["step_by_step_explanation"]
    return _check_contains_any(
        content,
        check_name="step_by_step_explanation",
        category=category,
        markers=markers,
    )


@pytest.mark.unit
def test_causal_prose_hash_map_answer_no_longer_vetoed() -> None:
    """The exact reproduction case from OMN-19401 must now pass."""
    assert _run_step_by_step_check(_HASH_MAP_RESPONSE) is None


@pytest.mark.unit
def test_literal_step_markers_still_pass() -> None:
    """The original four markers keep working -- this is additive, not a rewrite."""
    for content in (
        "Step 1: do X. Step 2: do Y.",
        "First, do X. Then do Y.",
        "1. Do X. 2. Do Y.",
    ):
        assert _run_step_by_step_check(content) is None


@pytest.mark.unit
def test_causal_connective_markers_pass_individually() -> None:
    for connective in (
        "because",
        "therefore",
        "consequently",
        "since",
        "as a result",
        "this means",
    ):
        content = f"X happens {connective} of Y."
        assert _run_step_by_step_check(content) is None, connective


@pytest.mark.unit
def test_response_with_no_marker_at_all_is_still_vetoed() -> None:
    """The check remains reject-only: content with none of the markers still fails."""
    result = _run_step_by_step_check(
        "Hash maps are fast on average and slower in the worst case."
    )
    assert result == "TASK_MISMATCH: failed step_by_step_explanation"
