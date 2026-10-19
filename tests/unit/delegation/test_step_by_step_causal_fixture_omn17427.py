# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: correct causal answers the step_by_step_explanation check vetoed.

OMN-19401 taught the check six causal connectives. Each answer below is a
correct explanation that walks its mechanism through with a connective outside
those six ("thus", "hence", ", so", "which means", "leads to", "due to",
"results in", "in turn", "which is why", "that is why", "as a consequence",
"causes"), and none of them carries "step", "1.", "first" or "then", so the
substring check refused every one. The negative controls are answers that
assert without explaining, plus prose that holds a new connective's letters
inside another word ("enthusiasm", ", sometimes"): those must still fail.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _HEURISTIC_CONTAINS_ANY_CHECKS,
    _check_contains_any,
)

pytestmark = pytest.mark.unit

_VETO = "TASK_MISMATCH: failed step_by_step_explanation"

_CORRECT_CAUSAL_ANSWERS: dict[str, str] = {
    "thus": (
        "A garbage collector frees only objects that nothing references. A "
        "long-lived cache that keeps adding entries holds a reference to each "
        "of them, thus they are never collected and memory use grows until "
        "the process is killed."
    ),
    "hence": (
        "Each comparison discards half of the remaining sorted range. Hence "
        "the range shrinks from n elements to one after about log2(n) "
        "comparisons, which is the cost of a binary search."
    ),
    "comma_so": (
        "A bond pays a fixed coupon. When market rates rise, new bonds pay "
        "more, so buyers will only take the old bond at a discount and its "
        "price falls until its yield matches the market."
    ),
    "which_means": (
        "An index keeps a column's values in a sorted B-tree, letting the "
        "engine jump to matching rows without reading the whole table. "
        "Writes have to update that tree as well, which means inserts get "
        "slower as more indexes are added."
    ),
    "leads_to": (
        "Water molecules in ice lock into a hexagonal lattice held by "
        "hydrogen bonds, leaving more space between them than in liquid "
        "water. That lower density leads to ice floating on water."
    ),
    "due_to": (
        "The two threads stall forever due to a circular wait: A holds lock "
        "X and waits for Y, while B holds Y and waits for X, and neither "
        "releases what it holds."
    ),
    "results_in": (
        "Earth's axis is tilted about 23.5 degrees relative to its orbit. "
        "For half the year a hemisphere leans toward the Sun and gets more "
        "direct sunlight over longer days, which results in summer there."
    ),
    "in_turn": (
        "A popular cache key expires and every request misses at once. Each "
        "miss queries the database, which in turn slows every other query, "
        "and the slower queries keep the cache empty for longer."
    ),
    "which_is_why": (
        "TCP numbers every segment and waits for acknowledgements, "
        "retransmitting any segment that goes missing. That bookkeeping adds "
        "round trips, which is why UDP, which skips it, has lower latency "
        "for voice calls."
    ),
    "that_is_why": (
        "A vaccine exposes the immune system to a harmless piece of a "
        "pathogen. Memory cells formed during that exposure recognise the "
        "real pathogen later, and that is why a vaccinated person clears the "
        "infection faster."
    ),
    "as_a_consequence": (
        "Floating point stores numbers as binary fractions and 0.3 has no "
        "exact binary form. As a consequence, adding the rounded values of "
        "0.1 and 0.2 lands slightly above 0.3."
    ),
    "causes": (
        "A heavy query holds a row lock for seconds. Every other transaction "
        "touching that row queues behind it, which causes request latency to "
        "spike across the service."
    ),
    # Verbatim Qwen3.8-27B answer from lab delegation run
    # 16de1829-24b4-4563-919f-2a19d28b2d88 (2026-10-19) to "Explain why a
    # database index makes reads faster but writes slower, in under 80 words
    # of plain prose. Do not use a list."
    "lab_qwen_database_index": (
        "A database index is an extra ordered structure that lets the database "
        "find matching rows without scanning every row, so reads can locate "
        "data quickly. However, every insert, update, or delete must also "
        "update that structure, preserving its order and consistency. That "
        "extra maintenance adds work to each write, making writes slower even "
        "though reads become faster."
    ),
}

_NON_EXPLANATIONS: dict[str, str] = {
    "bare_assertion": (
        "Hash map lookups are constant time on average and linear in the worst case."
    ),
    "list_of_facts": "TCP is reliable. UDP is fast. Both are transport protocols.",
    "intensifier_so": "Hash maps are so fast that everyone uses them.",
    "thus_inside_a_word": "Engineers greet hash maps with enthusiasm.",
    "comma_so_inside_a_word": "Hash maps, sometimes called dictionaries, store values by key.",
    "refusal": "I cannot answer that question.",
}


def _run_step_by_step_check(content: str) -> str | None:
    category, markers = _HEURISTIC_CONTAINS_ANY_CHECKS["step_by_step_explanation"]
    return _check_contains_any(
        content,
        check_name="step_by_step_explanation",
        category=category,
        markers=markers,
    )


@pytest.mark.parametrize(
    "content",
    list(_CORRECT_CAUSAL_ANSWERS.values()),
    ids=list(_CORRECT_CAUSAL_ANSWERS),
)
def test_correct_causal_answer_passes_step_by_step_explanation(content: str) -> None:
    assert _run_step_by_step_check(content) is None


@pytest.mark.parametrize(
    "content",
    list(_CORRECT_CAUSAL_ANSWERS.values()),
    ids=list(_CORRECT_CAUSAL_ANSWERS),
)
def test_fixture_answer_carries_none_of_the_omn19401_markers(content: str) -> None:
    """Each fixture answer was vetoed by the ten markers OMN-19401 shipped."""
    omn19401_markers = (
        "step",
        "1.",
        "first",
        "then",
        "because",
        "therefore",
        "consequently",
        "since",
        "as a result",
        "this means",
    )
    assert (
        _check_contains_any(
            content,
            check_name="step_by_step_explanation",
            category="TASK_MISMATCH",
            markers=omn19401_markers,
        )
        == _VETO
    )


@pytest.mark.parametrize(
    "content",
    list(_NON_EXPLANATIONS.values()),
    ids=list(_NON_EXPLANATIONS),
)
def test_non_explanation_is_still_vetoed(content: str) -> None:
    assert _run_step_by_step_check(content) == _VETO


def test_connective_at_line_start_passes() -> None:
    content = (
        "Each comparison discards half of the remaining sorted range.\n"
        "Thus the search ends after about log2(n) comparisons."
    )
    assert _run_step_by_step_check(content) is None
