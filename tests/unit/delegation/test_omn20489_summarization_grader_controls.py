# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Controls for the OMN-20489 summarization grader changes.

The fixture-driven agreement tests are in
``test_omn20489_summarization_grader_agreement.py``. These pin what must NOT
move: a real disclaimer still vetoes, the bare word is still a disclaimer for
every class but summarization, only summarization carries the new checks, and the
sentence counter reads a list item as a sentence and a semicolon run-on as one.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _check_accurate,
    _check_accurate_summary,
    _check_within_sentence_limit,
    _count_prose_sentences,
    _sentence_limit,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "answer",
    [
        "I cannot verify these figures, but the job passed.",
        "This is not verified against the log.",
        "The numbers may be inaccurate.",
    ],
)
def test_real_disclaimers_still_veto(answer: str) -> None:
    assert _check_accurate_summary(answer, None) is not None
    assert _check_accurate(answer, None) is not None


def test_bare_unverified_vetoes_other_classes_still() -> None:
    answer = "The scan found 0 unverified secrets and 3 unverified findings."
    assert _check_accurate(answer, None) is not None
    assert _check_accurate_summary(answer, None) is None


def test_only_summarization_uses_the_new_checks() -> None:
    limited = "Summarize this in at most 4 sentences. " + "word " * 200
    _, heuristic = resolve_task_class_dod_checks("summarization", prompt=limited)
    assert "accurate_summary" in heuristic
    assert "within_sentence_limit" in heuristic
    assert "accurate" not in heuristic
    for task_class in ("document", "research", "planning"):
        _, other = resolve_task_class_dod_checks(task_class, prompt=limited)
        assert "accurate_summary" not in other
        assert "within_sentence_limit" not in other


def test_a_prompt_with_no_stated_limit_waives_the_sentence_rule() -> None:
    unlimited = "Summarize this for a reviewer. " + "word " * 200
    _, heuristic = resolve_task_class_dod_checks("summarization", prompt=unlimited)
    assert "accurate_summary" in heuristic
    assert "within_sentence_limit" not in heuristic


@pytest.mark.parametrize(
    ("prompt", "limit"),
    [
        ("Summarize in at most 4 sentences.", 4),
        ("Summarize this in at most five sentences for a reviewer.", 5),
        ("Reply with no more than 2 sentences.", 2),
        ("Summarize this in at most one sentence.", 1),
        ("Summarize this for a reviewer.", None),
        ("", None),
        (None, None),
    ],
)
def test_sentence_limit_is_read_from_the_prompt(
    prompt: str | None, limit: int | None
) -> None:
    assert _sentence_limit(prompt) == limit


@pytest.mark.parametrize(
    ("answer", "count"),
    [
        ("One sentence only.", 1),
        ("First. Second! Third?", 3),
        # a semicolon run-on is one sentence
        ("It added the node; it wired the topic; it passed the test.", 1),
        # versions, file names and numbers hold dots that end nothing
        ("Release v0.4.208 changed `ci.yml` and 7.61s of runtime. Done.", 2),
        ("It did so, e.g. by retrying. Then it stopped.", 2),
        # every list item is a sentence, as is each sentence within one
        ("- Fixed the wait.\n- Fixed the fold.\n- Opened dev.", 3),
        ("1. Fixed the wait. It was slow.\n2. Opened dev.", 3),
        # a soft-wrapped line continues its paragraph
        ("This sentence wraps\nonto a second line.", 1),
        ("", 0),
    ],
)
def test_sentences_are_counted_as_a_reader_counts_them(answer: str, count: int) -> None:
    assert _count_prose_sentences(answer) == count


def test_sentence_limit_check_passes_without_a_stated_limit() -> None:
    assert _check_within_sentence_limit("A. B. C. D. E. F.", "Summarize this.") is None
    assert _check_within_sentence_limit("A. B. C. D. E. F.", None) is None
    assert _check_within_sentence_limit("A. B. C. D.", "in at most 4 sentences") is None
    reason = _check_within_sentence_limit("A. B. C. D. E.", "in at most 4 sentences")
    assert reason is not None
    assert "5 sentences" in reason
