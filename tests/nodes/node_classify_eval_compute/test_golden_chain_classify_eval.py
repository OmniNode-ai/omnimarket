# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: a fixed ledger builds the exact labelled set, and fixed replies score to the exact table."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from omnimarket.models.eval_classify.enum_classify import (
    EnumClassifyOperation,
    EnumIntegrationClass,
)
from omnimarket.models.eval_classify.model_classify_eval import (
    ModelClassifyEvalRequest,
    ModelClassifyEvalResult,
)
from omnimarket.nodes.node_classify_eval_compute.handlers.handler_classify_eval import (
    HandlerClassifyEval,
)
from tests.nodes.node_classify_eval_compute.support import (
    LEDGER_TEXT,
    answer,
    overlay,
    task,
)

pytestmark = pytest.mark.unit

C = EnumIntegrationClass
CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_classify_eval_compute/contract.yaml"
)


def _build() -> ModelClassifyEvalResult:
    return HandlerClassifyEval(overlay=overlay()).handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.BUILD_SET,
            suite_id="s",
            ledger_text=LEDGER_TEXT,
        )
    )


def _independent_order(ids: list[str]) -> list[str]:
    return sorted(ids, key=lambda i: hashlib.sha256(f"s:{i}".encode()).hexdigest())


def test_golden_build_draws_the_exact_ids_per_class() -> None:
    result = _build()
    assert result.failure is None
    expected = {
        C.NOT_RUNNING: _independent_order(
            [
                "repo-a#1",
                "repo-a#2",
                "overlays:" + hashlib.sha256(b"nr no number").hexdigest()[:10],
            ]
        )[:2],
        C.BLOCKED: ["repo-b#3"],
        C.DEFECT: ["repo-b#4"],
        C.SEEN_RUNNING: _independent_order(["repo-c#5", "repo-c#6", "repo-c#7"])[:2],
        C.RUN_NOT_SEEN: _independent_order(["repo-d#8", "repo-d#9", "repo-d#10"])[:2],
    }
    got: dict[EnumIntegrationClass, list[str]] = {}
    for t in result.tasks:
        got.setdefault(t.label, []).append(t.task_id)
    assert got == expected
    assert len(result.tasks) == 8
    assert {c.label: (c.available, c.drawn) for c in result.class_counts} == {
        C.NOT_RUNNING: (3, 2),
        C.BLOCKED: (1, 1),
        C.DEFECT: (1, 1),
        C.SEEN_RUNNING: (3, 2),
        C.RUN_NOT_SEEN: (3, 2),
    }
    assert result.rubric == overlay().rubric


def test_golden_build_carries_no_answer_cell_and_is_repeatable() -> None:
    first, second = _build(), _build()
    assert first == second
    for t in first.tasks:
        text = " ".join([t.title, t.runs, t.evidence, t.merged_at])
        for cell in ("state:", "step:", "owner:", "NOT RUNNING", "UNPROVEN"):
            assert cell not in text
    by_id = {t.task_id: t for t in first.tasks}
    assert by_id["repo-b#3"].evidence == "gap text of blocked one"
    assert by_id["repo-b#3"].runs == "dev runtime"


def test_golden_score_gives_the_exact_table() -> None:
    tasks = (
        task("t1", C.NOT_RUNNING),
        task("t2", C.NOT_RUNNING),
        task("t3", C.SEEN_RUNNING),
        task("t4", C.BLOCKED),
    )
    answers = (
        answer("t1", "reasoning\nnot_running", seconds=2.0),
        answer("t2", "mentions seen_running first\nblocked", seconds=4.0),
        answer("t3", "seen running", seconds=6.0),
        answer("t4", "no class named here", seconds=8.0),
        answer("t1", "not_running", engine="e2", seconds=1.0),
        answer("t3", "not_running", engine="e2", seconds=3.0),
    )
    result = HandlerClassifyEval().handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.SCORE_ANSWERS,
            suite_id="s",
            tasks=tasks,
            answers=answers,
        )
    )
    assert result.failure is None
    e1, e2 = result.scores
    assert (e1.engine, e1.n, e1.correct, e1.unparsed, e1.accuracy, e1.mean_seconds) == (
        "e1",
        4,
        2,
        1,
        0.5,
        5.0,
    )
    assert {c.label: (c.n, c.correct, c.accuracy) for c in e1.by_class} == {
        C.NOT_RUNNING: (2, 1, 0.5),
        C.SEEN_RUNNING: (1, 1, 1.0),
        C.BLOCKED: (1, 0, 0.0),
    }
    assert {(c.label, c.predicted, c.count) for c in e1.confusion} == {
        (C.NOT_RUNNING, C.NOT_RUNNING, 1),
        (C.NOT_RUNNING, C.BLOCKED, 1),
        (C.SEEN_RUNNING, C.SEEN_RUNNING, 1),
        (C.BLOCKED, None, 1),
    }
    assert (e2.engine, e2.n, e2.correct, e2.accuracy, e2.mean_seconds) == (
        "e2",
        2,
        1,
        0.5,
        2.0,
    )


def test_golden_contract_declares_the_topics() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.classify-eval-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.classify-eval-completed.v1"
    ]
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.classify-eval-completed.v1"
    )
    assert contract["handler"]["class"] == "HandlerClassifyEval"
