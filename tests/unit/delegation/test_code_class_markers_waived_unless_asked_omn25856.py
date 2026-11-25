# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A correct short code answer is not refused for lacking a tooling word (OMN-25856).

`follows_codebase_conventions` looked for pytest/ruff/typing/typed/contract in the
answer and `no_obvious_regressions` for regression/backward/compatib/existing
tests/no break. Clean code contains neither set, so a correct trivial answer was
labelled TASK_MISMATCH. As with `covers_edge_cases` (OMN-19401), each rule now
applies only when the prompt itself asks for what it judges. The real checks
(named tests run against the answer, compiles, final artifact only) are unchanged:
an answer that deletes the tests or breaks a named interface is still refused.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.delegation.graded_ladder.graders import extract_code_block
from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_named_test_run_request import (
    EnumNamedTestRunStatus,
    ModelNamedTestRunRequest,
    ModelNamedTestRunResult,
    ModelNamedTestSpec,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

CLASSES = ("code_generation", "refactor")
PLAIN = "Add two ints in add_impl.py as add(a, b)."
CORRECT = "def add(a, b):\n    return a + b"
# Breaks the named interface: `add` is gone.
BREAKS_INTERFACE = "def plus(a, b):\n    return a + b"
_TEST_ID = "test_add.py::test_add"
_MARKER_REASONS = (
    "failed follows_codebase_conventions",
    "failed no_obvious_regressions",
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "test_add.py").write_text(
        "from add_impl import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    return root


def _run_named_tests(
    workspace: Path, answer: str, answer_path: str
) -> ModelNamedTestRun:
    """Run the named test for real against the answer, in a copy of the workspace."""
    request = ModelNamedTestRunRequest(
        spec=ModelNamedTestSpec(
            workspace_root=str(workspace),
            answer_path=answer_path,
            test_ids=(_TEST_ID,),
        ),
        answer_content=answer,
    )
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "ws"
        shutil.copytree(request.spec.workspace_root, copy)
        (copy / answer_path).write_text(
            extract_code_block(answer).rstrip() + "\n", encoding="utf-8"
        )
        code = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", _TEST_ID],
            cwd=copy,
            capture_output=True,
            check=False,
            timeout=120,
        ).returncode
    result = ModelNamedTestRunResult(
        status=EnumNamedTestRunStatus.COMPLETED,
        run=ModelNamedTestRun(
            test_ids=(_TEST_ID,),
            failed_test_ids=(_TEST_ID,) if code else (),
            exit_code=code,
        ),
    )
    assert result.run is not None
    return result.run


def _gate(task_type: str, prompt: str, content: str, run: ModelNamedTestRun):
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, prompt=prompt)
    return delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=task_type,
            llm_response_content=content,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        named_test_run=run,
        named_tests_requested=True,
    )


def _reasons(result: object) -> str:
    return " | ".join(result.failure_reasons)  # type: ignore[attr-defined]


@pytest.mark.parametrize("task_type", CLASSES)
def test_correct_trivial_answer_passes_without_tooling_words(
    task_type: str, workspace: Path
) -> None:
    run = _run_named_tests(workspace, CORRECT, "add_impl.py")
    result = _gate(task_type, PLAIN, CORRECT, run)
    assert result.passed is True
    assert result.failure_reasons == ()
    rules = {e.rule for e in result.rule_evaluations}
    assert not rules & {"follows_codebase_conventions", "no_obvious_regressions"}


@pytest.mark.parametrize("task_type", CLASSES)
def test_untested_correct_answer_is_refused_only_for_missing_tests(
    task_type: str,
) -> None:
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, prompt=PLAIN)
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=task_type,
            llm_response_content=CORRECT,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        named_test_run=None,
        named_tests_requested=True,
    )
    assert result.passed is False
    assert "passes_existing_tests" in _reasons(result)
    assert not any(reason in _reasons(result) for reason in _MARKER_REASONS)


@pytest.mark.parametrize("task_type", CLASSES)
def test_answer_that_breaks_a_named_interface_is_refused(
    task_type: str, workspace: Path
) -> None:
    run = _run_named_tests(workspace, BREAKS_INTERFACE, "add_impl.py")
    result = _gate(task_type, PLAIN, BREAKS_INTERFACE, run)
    assert result.passed is False
    assert "passes_existing_tests" in _reasons(result)


@pytest.mark.parametrize("task_type", CLASSES)
def test_answer_that_deletes_the_tests_is_refused(
    task_type: str, workspace: Path
) -> None:
    # The answer overwrites the test file with a module that holds no tests.
    emptied = "# tests removed\n"
    run = _run_named_tests(workspace, emptied, "test_add.py")
    result = _gate(task_type, PLAIN, emptied, run)
    assert result.passed is False
    assert "passes_existing_tests" in _reasons(result)


@pytest.mark.parametrize("task_type", CLASSES)
@pytest.mark.parametrize(
    ("ask", "rule"),
    [
        ("Keep it typed and ruff clean.", "follows_codebase_conventions"),
        ("Keep it backward compatible.", "no_obvious_regressions"),
        ("Do not introduce a regression.", "no_obvious_regressions"),
        ("Follow the repo's pytest conventions.", "follows_codebase_conventions"),
    ],
)
def test_rule_still_applies_when_the_prompt_asks_for_it(
    task_type: str, ask: str, rule: str, workspace: Path
) -> None:
    run = _run_named_tests(workspace, CORRECT, "add_impl.py")
    result = _gate(task_type, f"{PLAIN} {ask}", CORRECT, run)
    # Passing tests accept the answer; the asked-for rule is still evaluated and
    # still fails an answer that carries none of its markers.
    evaluated = {e.rule: e.passed for e in result.rule_evaluations}
    assert evaluated.get(rule) is False


@pytest.mark.parametrize("task_type", CLASSES)
def test_rule_is_satisfied_when_asked_and_answer_carries_the_marker(
    task_type: str, workspace: Path
) -> None:
    answer = "def add(a: int, b: int) -> int:\n    # typed, backward compatible\n    return a + b"
    run = _run_named_tests(workspace, answer, "add_impl.py")
    prompt = f"{PLAIN} Keep it typed and backward compatible."
    result = _gate(task_type, prompt, answer, run)
    assert result.passed is True
    assert result.failure_reasons == ()
    evaluated = {e.rule: e.passed for e in result.rule_evaluations}
    assert evaluated["follows_codebase_conventions"] is True
    assert evaluated["no_obvious_regressions"] is True
