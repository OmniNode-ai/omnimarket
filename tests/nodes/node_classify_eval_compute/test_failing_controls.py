# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failing controls: each of these fails if the named behaviour is removed from the handler or the models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.eval_classify.enum_classify import (
    EnumClassifyOperation,
    EnumIntegrationClass,
)
from omnimarket.models.eval_classify.model_classify_eval import ModelClassifyEvalRequest
from omnimarket.nodes.node_classify_eval_compute.handlers.handler_classify_eval import (
    HandlerClassifyEval,
    parse_class,
)
from tests.nodes.node_classify_eval_compute.support import (
    LEDGER_TEXT,
    SECTION_LABELS,
    answer,
    overlay,
    task,
)

pytestmark = pytest.mark.unit

C = EnumIntegrationClass


def test_a_line_listed_under_two_classes_is_left_out_and_counted() -> None:
    text = (
        LEDGER_TEXT
        + "\n## DEFECT (again)\n"
        + "- repo-c 5 | seen one | merged 2026-11-06 02:00Z | runs: x | state: DEFECT | gap: g\n"
    )
    result = HandlerClassifyEval(overlay=overlay()).handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.BUILD_SET, suite_id="s", ledger_text=text
        )
    )
    assert result.failure is None, result.detail
    assert result.ambiguous_excluded == 1
    assert "repo-c#5" not in {t.task_id for t in result.tasks}
    counts = {c.label: c.available for c in result.class_counts}
    assert counts[C.SEEN_RUNNING] == 2
    assert counts[C.DEFECT] == 1


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("it was seen_running before\nnot_running", C.NOT_RUNNING),
        ("Not running", C.NOT_RUNNING),
        ("seen-running", C.SEEN_RUNNING),
        ("### ANSWER\nrun_not_seen", C.RUN_NOT_SEEN),
        ("blocked by another change; then defect", C.DEFECT),
        ("no class here", None),
        ("", None),
    ],
)
def test_parse_class_takes_the_last_class_word(reply: str, expected: C | None) -> None:
    assert parse_class(reply) is expected


def test_an_unparsed_reply_counts_wrong_and_is_counted() -> None:
    result = HandlerClassifyEval().handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.SCORE_ANSWERS,
            suite_id="s",
            tasks=(task("t1", C.BLOCKED),),
            answers=(answer("t1", "no class named"),),
        )
    )
    (score,) = result.scores
    assert (score.n, score.correct, score.unparsed, score.accuracy) == (1, 0, 1, 0.0)


def test_a_reply_is_never_credited_to_the_wrong_class() -> None:
    tasks = (task("a", C.BLOCKED), task("b", C.DEFECT), task("c", C.BLOCKED))
    answers = tuple(answer(t.task_id, "blocked") for t in tasks)
    result = HandlerClassifyEval().handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.SCORE_ANSWERS,
            suite_id="s",
            tasks=tasks,
            answers=answers,
        )
    )
    (score,) = result.scores
    assert {c.label: c.accuracy for c in score.by_class} == {
        C.BLOCKED: 1.0,
        C.DEFECT: 0.0,
    }


def test_an_overlay_whose_sizes_name_other_classes_is_refused() -> None:
    with pytest.raises(ValidationError):
        overlay(per_class_n={C.NOT_RUNNING: 1})


def test_an_overlay_size_below_one_is_refused() -> None:
    sizes = dict.fromkeys(SECTION_LABELS.values(), 1)
    sizes[C.BLOCKED] = 0
    with pytest.raises(ValidationError):
        overlay(per_class_n=sizes)


def test_a_line_with_only_answer_cells_carries_no_evidence() -> None:
    text = "\n".join(
        [
            "## NOT RUNNING (1)",
            "- repo-z 11 | t | merged 2026-11-05 01:41Z | runs: r | state: NOT RUNNING | step: s | owner: NONE",
            "## BLOCKED (1)",
            "- repo-z 12 | t | merged 2026-11-05 01:41Z | runs: r | state: BLOCKED | owner: NONE",
            "## DEFECT (1)",
            "- repo-z 13 | t | merged 2026-11-05 01:41Z | runs: r | state: DEFECT",
            "## SEEN RUNNING (1)",
            "- repo-z 14 | t | merged 2026-11-05 01:41Z | reading: seen",
            "## IN PLACE OR UNPROVEN (1)",
            "- repo-z 15 | t | merged 2026-11-05 01:41Z | state: IN PLACE | reading owed: owed",
        ]
    )
    result = HandlerClassifyEval(
        overlay=overlay(per_class_n=dict.fromkeys(C, 1), min_total=5)
    ).handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.BUILD_SET, suite_id="s", ledger_text=text
        )
    )
    assert result.failure is None, result.detail
    evidence = {t.task_id: t.evidence for t in result.tasks}
    assert evidence == {
        "repo-z#11": "",
        "repo-z#12": "",
        "repo-z#13": "",
        "repo-z#14": "seen",
        "repo-z#15": "owed",
    }
