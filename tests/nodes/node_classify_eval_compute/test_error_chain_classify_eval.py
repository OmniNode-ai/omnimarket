# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: every malformed request ends as a typed failure, never a set or a table."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.eval_classify.enum_classify import (
    EnumClassifyEvalFailure,
    EnumClassifyOperation,
    EnumIntegrationClass,
)
from omnimarket.models.eval_classify.model_classify_eval import (
    ModelClassifyAnswer,
    ModelClassifyEvalRequest,
    ModelClassifyEvalResult,
)
from omnimarket.models.eval_classify.model_classify_task import ModelClassifyEvalTask
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
F = EnumClassifyEvalFailure


def _build(
    text: str,
    per_class_n: dict[EnumIntegrationClass, int] | None = None,
    min_total: int = 8,
) -> ModelClassifyEvalResult:
    return HandlerClassifyEval(
        overlay=overlay(per_class_n=per_class_n, min_total=min_total)
    ).handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.BUILD_SET, suite_id="s", ledger_text=text
        )
    )


def _score(
    tasks: tuple[ModelClassifyEvalTask, ...],
    answers: tuple[ModelClassifyAnswer, ...],
) -> ModelClassifyEvalResult:
    return HandlerClassifyEval().handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.SCORE_ANSWERS,
            suite_id="s",
            tasks=tasks,
            answers=answers,
        )
    )


def _assert_failure(
    result: ModelClassifyEvalResult, code: EnumClassifyEvalFailure
) -> None:
    assert result.failure is code
    assert result.detail
    assert result.tasks == ()
    assert result.scores == ()


def test_error_chain_overlay_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    result = HandlerClassifyEval().handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.BUILD_SET,
            suite_id="s",
            ledger_text=LEDGER_TEXT,
        )
    )
    _assert_failure(result, F.OVERLAY_UNAVAILABLE)


def test_error_chain_no_ledger_lines() -> None:
    _assert_failure(_build("# nothing\n## COUNTS\n- x"), F.NO_LEDGER_LINES)


def test_error_chain_class_shortfall() -> None:
    result = _build(
        LEDGER_TEXT,
        per_class_n={
            C.NOT_RUNNING: 4,
            C.BLOCKED: 1,
            C.DEFECT: 1,
            C.SEEN_RUNNING: 2,
            C.RUN_NOT_SEEN: 2,
        },
    )
    _assert_failure(result, F.CLASS_SHORTFALL)
    assert "not_running" in result.detail


def test_error_chain_below_minimum_total() -> None:
    _assert_failure(_build(LEDGER_TEXT, min_total=9), F.BELOW_MINIMUM_TOTAL)


def test_error_chain_no_tasks() -> None:
    _assert_failure(_score((), (answer("t1", "blocked"),)), F.NO_TASKS)


def test_error_chain_no_answers() -> None:
    _assert_failure(_score((task("t1", C.BLOCKED),), ()), F.NO_ANSWERS)


def test_error_chain_unknown_task() -> None:
    _assert_failure(
        _score((task("t1", C.BLOCKED),), (answer("zz", "blocked"),)), F.UNKNOWN_TASK
    )


def test_error_chain_duplicate_task() -> None:
    result = _score(
        (task("t1", C.BLOCKED), task("t1", C.DEFECT)), (answer("t1", "blocked"),)
    )
    _assert_failure(result, F.DUPLICATE_TASK)


def test_error_chain_duplicate_answer() -> None:
    result = _score(
        (task("t1", C.BLOCKED),), (answer("t1", "blocked"), answer("t1", "defect"))
    )
    _assert_failure(result, F.DUPLICATE_ANSWER)


def test_error_chain_build_request_without_ledger_text_is_refused() -> None:
    with pytest.raises(ValueError, match="ledger_text"):
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.BUILD_SET, suite_id="s"
        )
