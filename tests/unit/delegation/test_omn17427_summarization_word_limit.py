# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""A word limit a summarization prompt states is a breach when the answer exceeds it.

Two delegation experiments on 2026-10-18 found the gate accepting answers that
broke a limit the prompt stated: 12 of 29 summaries asked for "at most 80 words"
were longer, and the gate accepted every one. The sentence limit was already
enforced; these tests pin the same rule for words.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _check_within_word_limit,
    _word_limit,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("prompt", "limit"),
    [
        ("Summarize this in at most 80 words.", 80),
        ("Reply with no more than 25 words.", 25),
        ("Summarize this in at most twelve words.", 12),
        ("Summarize this in at most 1 word.", 1),
        ("Summarize this for a reviewer.", None),
        ("Summarize in at most 4 sentences.", None),
        # a per-sentence ceiling is a different rule, not a total
        ("Keep every sentence at most 20 words per sentence.", None),
        ("", None),
        (None, None),
    ],
)
def test_word_limit_is_read_from_the_prompt(
    prompt: str | None, limit: int | None
) -> None:
    assert _word_limit(prompt) == limit


def test_word_limit_check_passes_without_a_stated_limit() -> None:
    long_answer = " ".join(["word"] * 300)
    assert _check_within_word_limit(long_answer, "Summarize this.") is None
    assert _check_within_word_limit(long_answer, None) is None


def test_word_limit_check_rejects_an_answer_over_the_limit() -> None:
    prompt = "Summarize the report in at most 80 words."
    at_limit = " ".join(["word"] * 80)
    over_limit = " ".join(["word"] * 81)
    assert _check_within_word_limit(at_limit, prompt) is None
    reason = _check_within_word_limit(over_limit, prompt)
    assert reason is not None
    assert reason.startswith("TASK_MISMATCH")
    assert "81 words" in reason
    assert "at most 80" in reason


def test_summarization_carries_the_word_limit_only_when_the_prompt_states_one() -> None:
    limited = "Summarize this in at most 80 words. " + "word " * 200
    _, heuristic = resolve_task_class_dod_checks("summarization", prompt=limited)
    assert "within_word_limit" in heuristic
    unlimited = "Summarize this for a reviewer. " + "word " * 200
    _, heuristic = resolve_task_class_dod_checks("summarization", prompt=unlimited)
    assert "within_word_limit" not in heuristic


def test_other_classes_do_not_carry_the_word_limit() -> None:
    limited = "Write this up in at most 80 words. " + "word " * 200
    for task_class in ("document", "research", "planning"):
        _, other = resolve_task_class_dod_checks(task_class, prompt=limited)
        assert "within_word_limit" not in other
