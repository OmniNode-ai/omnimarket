# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The build step's set from the committed ledger-shaped fixture is valid, and the set's counts match a recount.

The real labelled set is built from the integration ledger copy on a lab host, never committed (the ledger
names private repositories and this repository is public). This test builds the set from the committed
fixture in the same shape, so the build step's guarantees are held in CI.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from omnimarket.models.eval_classify.enum_classify import (
    EnumClassifyOperation,
    EnumIntegrationClass,
)
from omnimarket.models.eval_classify.model_classify_eval import ModelClassifyEvalRequest
from omnimarket.models.eval_classify.model_classify_task import ModelClassifyEvalTask
from omnimarket.nodes.node_classify_eval_compute.handlers.handler_classify_eval import (
    HandlerClassifyEval,
)
from tests.nodes.node_classify_eval_compute.support import overlay

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ledger_sample.md"
CODE_SUITE = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / "code_eval_suite"
    / "suite.v1.json"
)
C = EnumIntegrationClass


def _build() -> tuple[tuple[ModelClassifyEvalTask, ...], dict[C, int]]:
    result = HandlerClassifyEval(
        overlay=overlay(per_class_n=dict.fromkeys(C, 1), min_total=5)
    ).handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.BUILD_SET,
            suite_id="s",
            ledger_text=FIXTURE.read_text(encoding="utf-8"),
        )
    )
    assert result.failure is None, result.detail
    return result.tasks, {c.label: c.drawn for c in result.class_counts}


def test_the_set_holds_unique_tasks_of_every_class_and_its_counts_match_a_recount() -> (
    None
):
    tasks, drawn = _build()
    assert len({t.task_id for t in tasks}) == len(tasks)
    assert {t.label for t in tasks} == set(C)
    assert drawn == dict(Counter(t.label for t in tasks))


def test_no_task_text_carries_an_answer_cell() -> None:
    tasks, _ = _build()
    for t in tasks:
        text = " ".join([t.title, t.runs, t.evidence, t.merged_at])
        for cell in ("state:", "step:", "owner:"):
            assert cell not in text, t.task_id


def test_the_code_suite_document_is_not_a_classify_set() -> None:
    # the code eval census reads suite.v1.json by name; a classify set must never be put there
    text = CODE_SUITE.read_text(encoding="utf-8")
    assert "classify_integration_line" not in text
