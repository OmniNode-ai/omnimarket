# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Leak mask: class words in a task's text cells are replaced by the overlay's mask text."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.eval_classify.enum_classify import (
    EnumClassifyOperation,
    EnumIntegrationClass,
)
from omnimarket.models.eval_classify.model_classify_eval import ModelClassifyEvalRequest
from omnimarket.models.eval_classify.model_classify_overlay import (
    ModelClassifyEvalOverlay,
)
from omnimarket.nodes.node_classify_eval_compute.handlers.handler_classify_eval import (
    HandlerClassifyEval,
)
from tests.nodes.node_classify_eval_compute.support import SECTION_LABELS, overlay

pytestmark = pytest.mark.unit

C = EnumIntegrationClass


def _ledger_with_defect_evidence() -> str:
    return "\n".join(
        [
            "## NOT RUNNING (2)",
            "- repo-a 1 | nr one | merged 2026-11-05 01:41Z | runs: dev runtime | state: NOT RUNNING | gap: gap text of nr one | step: step text | owner: NONE",
            "- repo-a 2 | nr two | merged 2026-11-05 01:41Z | runs: dev runtime | state: NOT RUNNING | gap: gap text of nr two | step: step text | owner: NONE",
            "## BLOCKED (1)",
            "- repo-b 3 | blocked one | merged 2026-11-05 01:41Z | runs: dev runtime | state: BLOCKED | gap: gap text of blocked one | step: step text | owner: NONE",
            "## DEFECT (1)",
            "- repo-b 4 | defect one | merged 2026-11-05 01:41Z | runs: dev runtime | state: DEFECT | gap: DEFECT second copy | step: step text | owner: NONE",
            "## SEEN RUNNING (2)",
            "- repo-c 5 | seen one | merged 2026-11-06 02:00Z | reading: reading text of seen one",
            "- repo-c 6 | seen two | merged 2026-11-06 02:00Z | reading: reading text of seen two",
            "## IN PLACE OR UNPROVEN (2)",
            "- repo-d 8 | rns one | merged 2026-11-07 03:00Z | runs: skills | state: UNPROVEN | reading owed: owed text of rns one",
            "- repo-d 9 | rns two | merged 2026-11-07 03:00Z | runs: skills | state: IN PLACE | reading owed: owed text of rns two",
        ]
    )


def test_golden_leak_mask_replaces_class_words_in_evidence() -> None:
    ov = overlay()
    result = HandlerClassifyEval(overlay=ov).handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.BUILD_SET,
            suite_id="s",
            ledger_text=_ledger_with_defect_evidence(),
        )
    )
    assert result.failure is None, result.detail
    by_id = {t.task_id: t for t in result.tasks}
    defect_task = by_id["repo-b#4"]
    assert "[class word] second copy" in defect_task.evidence
    assert "DEFECT" not in defect_task.evidence
    assert result.masked_cells == 1


def test_failing_control_never_matching_phrase_leaves_text_untouched() -> None:
    ov = ModelClassifyEvalOverlay(
        section_labels=SECTION_LABELS,
        per_class_n={
            C.NOT_RUNNING: 2,
            C.BLOCKED: 1,
            C.DEFECT: 1,
            C.SEEN_RUNNING: 2,
            C.RUN_NOT_SEEN: 2,
        },
        seed="s",
        min_total=8,
        rubric="Classify one merged change. Last line: the class word alone.",
        leak_phrases=("ZZZ",),
        leak_mask="[class word]",
    )
    result = HandlerClassifyEval(overlay=ov).handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.BUILD_SET,
            suite_id="s",
            ledger_text=_ledger_with_defect_evidence(),
        )
    )
    assert result.failure is None, result.detail
    by_id = {t.task_id: t for t in result.tasks}
    defect_task = by_id["repo-b#4"]
    assert "DEFECT" in defect_task.evidence
    assert result.masked_cells == 0


def test_empty_leak_phrases_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelClassifyEvalOverlay(
            section_labels=SECTION_LABELS,
            per_class_n={
                C.NOT_RUNNING: 2,
                C.BLOCKED: 1,
                C.DEFECT: 1,
                C.SEEN_RUNNING: 2,
                C.RUN_NOT_SEEN: 2,
            },
            seed="s",
            min_total=8,
            rubric="r",
            leak_phrases=(),
            leak_mask="[class word]",
        )


def test_empty_leak_mask_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelClassifyEvalOverlay(
            section_labels=SECTION_LABELS,
            per_class_n={
                C.NOT_RUNNING: 2,
                C.BLOCKED: 1,
                C.DEFECT: 1,
                C.SEEN_RUNNING: 2,
                C.RUN_NOT_SEEN: 2,
            },
            seed="s",
            min_total=8,
            rubric="r",
            leak_phrases=("DEFECT",),
            leak_mask="",
        )
