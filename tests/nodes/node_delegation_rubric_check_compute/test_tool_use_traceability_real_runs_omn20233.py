# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""task_answer_traceable against the 20 real runs it failed in the first scored batch (OMN-20233).

Each fixture is trimmed to the windows of the run that bear on the answer's
tokens, with local paths, lab host names and addresses replaced. Every run was
classified by hand: ``a`` is a real untraceable claim (a misquoted task, a
method name the run never saw, a status row nothing printed, a file no call
touched); ``b`` failed only on a shape (an elided call, a ``path:line``
citation of a line the run read, a refused command, a quote with its sentence's
full stop). The fix must pass every ``b`` and still fail every ``a`` on its
real claim, and the extraction must still pull the ``b`` shapes out, so the
fixtures keep testing the shapes and not an empty answer.
"""

import json
from pathlib import Path

import pytest

from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_summarization import (
    _missing,
    answer_units,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_tool_use import (
    task_answer_traceable,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    ModelRubricCheckRequest,
    ModelToolCall,
    ModelToolCallResult,
    ModelToolUseTranscript,
)

pytestmark = pytest.mark.unit

CASES = json.loads(
    (
        Path(__file__).parent / "fixtures" / "tool_use_traceability_real_runs.json"
    ).read_text(encoding="utf-8")
)
RUBRIC = load_delegation_class_rubrics().for_class("tool_use")
CRITERION = next(
    row for row in RUBRIC.criteria if row.criterion_id == "task_answer_traceable"
)


def request_of(case: dict[str, object]) -> ModelRubricCheckRequest:
    calls = tuple(
        ModelToolCall(
            call_id=str(row["call_id"]),
            tool_name=str(row["tool"]),
            arguments_json=str(row["arguments_json"]),
            result=None
            if row["status"] is None
            else ModelToolCallResult(status=row["status"], output=str(row["output"])),
        )
        for row in case["calls"]  # type: ignore[attr-defined]
    )
    return ModelRubricCheckRequest(
        task_class="tool_use",
        request_text=str(case["request_text"]),
        answer_text=str(case["answer_text"]),
        rubric=RUBRIC,
        transcript=ModelToolUseTranscript(
            declared_tools=(), tool_calls=calls, turn_count=1
        ),
    )


def ids(case: dict[str, object]) -> str:
    return f"{case['source']}-{case['run']}-{case['classification']}"


def test_fixture_covers_the_whole_failing_batch():
    assert len(CASES) == 20
    assert sum(case["classification"] == "a" for case in CASES) == 4


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["classification"] == "b"], ids=ids
)
def test_shape_only_failure_now_passes(case):
    row = task_answer_traceable(request_of(case), CRITERION)
    assert (row.outcome, row.reason_code) == ("PASS", "answer_traceable"), row.detail


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["classification"] == "a"], ids=ids
)
def test_real_untraceable_claim_still_fails_on_that_claim(case):
    row = task_answer_traceable(request_of(case), CRITERION)
    assert row.outcome == "FAIL"
    assert row.facts == tuple(case["after"]["untraceable"])


@pytest.mark.parametrize("case", CASES, ids=ids)
def test_extraction_still_finds_what_the_old_matcher_flagged(case):
    """Every token the first scoring flagged is still extracted and checked."""
    extracted = {
        token
        for unit in answer_units(str(case["answer_text"]))
        for token in _missing(unit, "", CRITERION.params)
    }
    assert set(case["before_untraceable"]) <= extracted
