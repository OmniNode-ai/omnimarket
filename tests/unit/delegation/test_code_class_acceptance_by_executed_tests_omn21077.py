# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A code answer is judged by whether it works, on both delegation paths (OMN-21077).

For a code class (``execution_required: true``) acceptance is every deterministic
check passed AND ``passes_existing_tests`` ran and passed. The graded score, and
the keyword heuristics inside it, decide nothing. No named test run means the
answer was not checked, and it is refused as INSUFFICIENT. A rubric-scored class
keeps its bar.
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.delegation.graded_ladder.graders import extract_code_block
from omnimarket.inference.task_class_authority import (
    resolve_task_class_execution_required,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_named_test_run_request import (
    NAMED_TESTS_ANSWER_PATH_METADATA_KEY,
    NAMED_TESTS_METADATA_KEY,
    NAMED_TESTS_WORKSPACE_METADATA_KEY,
    EnumNamedTestRunStatus,
    ModelNamedTestRunRequest,
    ModelNamedTestRunResult,
    ModelNamedTestSpec,
)
from omnimarket.models.delegation.wire.model_quality_gate import (
    INSUFFICIENT_EVIDENCE_VERDICT_PREFIX,
    SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
    EnumQualityGateCategory,
    ModelQualityGateResult,
    ModelQualityRuleEvaluation,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill as skill_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hw,
)
from omnimarket.nodes.node_delegation_orchestrator.models import (
    ModelDelegationCompleted,
    ModelDelegationFailed,
    ModelDelegationRequest,
    ModelInferenceResponseData,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
    resolve_required_bar_authority,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

PROMPT = "add two ints"
CODE_CLASSES = ("code_generation", "refactor", "test", "validator_generation")

# The four answers of the lab probes. None carries a keyword the heuristics
# look for except STUB_TYPING, which carries `typing` on purpose.
CORRECT = "def add(a, b):\n    return a + b"
STUB = "def add(a, b):\n    pass"
STUB_TYPING = (
    "import typing\n\n\ndef add(a: typing.Any, b: typing.Any) -> typing.Any:\n    pass"
)
WRONG = "def add(a, b):\n    return a - b"
TEST_ANSWER = (
    "import pytest\n\n\n@pytest.mark.unit\ndef test_add() -> None:\n"
    "    assert 2 + 3 == 5"
)
_TEST_ID = "test_add.py::test_add"
_LOCAL_BACKEND = ModelResolvedDelegationBackend(
    backend_id="local-coder",
    model_id="Qwen3.6-35B-A3B",
    endpoint_ref="https://local.example/v1/chat/completions",
    tier="local",
    max_tokens=4096,
    timeout_ms=30000,
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


def _spec(workspace: Path) -> ModelNamedTestSpec:
    return ModelNamedTestSpec(
        workspace_root=str(workspace),
        answer_path="add_impl.py",
        test_ids=(_TEST_ID,),
    )


def _pytest_runner(request: ModelNamedTestRunRequest) -> ModelNamedTestRunResult:
    """Run the named tests against the answer, in a copy of the workspace.

    A test double for the named-test runner the local port is given: it writes
    the answer's code to the named file in a copy and runs each named pytest id
    for real, so the verdict comes from the code, not from a fixed outcome.
    """
    spec = request.spec
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "ws"
        shutil.copytree(spec.workspace_root, copy)
        (copy / spec.answer_path).write_text(
            extract_code_block(request.answer_content).rstrip() + "\n",
            encoding="utf-8",
        )
        failed = tuple(
            test_id
            for test_id in spec.test_ids
            if subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    test_id,
                ],
                cwd=copy,
                capture_output=True,
                check=False,
                timeout=120,
            ).returncode
            != 0
        )
    return ModelNamedTestRunResult(
        status=EnumNamedTestRunStatus.COMPLETED,
        run=ModelNamedTestRun(
            test_ids=spec.test_ids, failed_test_ids=failed, exit_code=1 if failed else 0
        ),
    )


def _fenced(code: str) -> str:
    return f"### ANSWER\n```python\n{code}\n```"


def _no_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        port_mod, "resolve_delegation_backend", lambda *_a, **_k: _LOCAL_BACKEND
    )
    monkeypatch.setattr(port_mod, "next_eligible_tier", lambda *_a, **_k: None)
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda _backend_id: "local")
    monkeypatch.setattr(
        port_mod, "resolve_task_class_max_escalations", lambda _task_type: 0
    )
    monkeypatch.setattr(hw, "next_eligible_tier", lambda *_a, **_k: None)
    monkeypatch.setattr(hw, "tier_max_retries", lambda _tier: 0)
    monkeypatch.setattr(hw, "is_free_tier", lambda _tier: False)


def _effect_returning(content: str) -> port_mod._EffectHandler:
    def _effect(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=content,
            tokens_in=10,
            tokens_out=20,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )

    return _effect


def _dispatch(
    port: LocalDelegationDispatchPort,
    *,
    task_type: str,
    named_tests: ModelNamedTestSpec | None,
) -> dict[str, object]:
    return asyncio.run(
        port.dispatch(
            prompt=PROMPT,
            task_type=task_type,
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
            named_tests=named_tests,
        )
    )


def _bus_workflow(
    *,
    task_type: str,
    content: str,
    response_contract: dict[str, object] | None = None,
) -> tuple[hw.HandlerDelegationWorkflow, ModelQualityGateIntent]:
    handler = hw.HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt=PROMPT,
            task_type=task_type,
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
            response_contract=response_contract,
        )
    )
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, prompt=PROMPT)
    handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=cid,
            task_type=task_type,
            selected_model="fixture-model",
            selected_backend_id=uuid4(),
            selected_backend_ref="fixture-backend",
            endpoint_url="http://provider.invalid:8000",
            cost_tier="low",
            max_context_tokens=65536,
            max_tokens=2048,
            system_prompt="Return the requested artifact.",
            rationale="Controlled single-tier route for code-class acceptance.",
            tier_name="local",
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        )
    )
    contract = handler.workflows[cid].effective_deliverable_contract
    assert contract is not None
    events = handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content=(
                f"{contract.render_start_marker}\n{content}"
                if contract.render_start_marker
                else content
            ),
            model_used="fixture-model",
            latency_ms=10,
        )
    )
    assert handler.workflows[cid].state is EnumDelegationState.INFERENCE_COMPLETED
    intent = next(
        event for event in events if isinstance(event, ModelQualityGateIntent)
    )
    return handler, intent


def _bus_accepts(task_type: str, result: ModelQualityGateResult) -> bool:
    handler, intent = _bus_workflow(task_type=task_type, content=STUB)
    result = result.model_copy(update={"correlation_id": intent.payload.correlation_id})
    return any(
        isinstance(event, ModelDelegationCompleted)
        for event in handler.handle_gate_result(result)
    )


def _local_accepts(task_type: str, result: ModelQualityGateResult) -> bool:
    port = LocalDelegationDispatchPort.__new__(LocalDelegationDispatchPort)
    return port._is_quality_accepted(task_type, result)


def _gate(task_type: str, content: str, run: ModelNamedTestRun | None):
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, prompt=PROMPT)
    from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
        ModelQualityGateInput,
    )

    return delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=task_type,
            llm_response_content=content,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        named_test_run=run,
    )


_PASSED_RUN = ModelNamedTestRun(test_ids=(_TEST_ID,), exit_code=0)
_FAILED_RUN = ModelNamedTestRun(
    test_ids=(_TEST_ID,), failed_test_ids=(_TEST_ID,), exit_code=1
)


# -- which classes are code classes -------------------------------------------


@pytest.mark.parametrize("task_type", CODE_CLASSES)
def test_code_classes_are_execution_required_and_name_their_tests(
    task_type: str,
) -> None:
    assert resolve_task_class_execution_required(task_type) is True
    assert resolve_required_bar_authority(
        task_type=task_type
    ).acceptance_by_executed_tests
    deterministic, _ = resolve_task_class_dod_checks(task_type)
    assert "passes_existing_tests" in deterministic


@pytest.mark.parametrize("task_type", ["document", "documentation", "review"])
def test_rubric_classes_are_not_code_classes(task_type: str) -> None:
    assert resolve_task_class_execution_required(task_type) is False
    assert not resolve_required_bar_authority(
        task_type=task_type
    ).acceptance_by_executed_tests


# -- the gate ------------------------------------------------------------------


@pytest.mark.parametrize("answer", [CORRECT, STUB, STUB_TYPING])
@pytest.mark.parametrize("task_type", ["code_generation", "refactor"])
def test_gate_refuses_an_untested_code_answer_as_insufficient(
    task_type: str, answer: str
) -> None:
    result = _gate(task_type, answer, None)
    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert any(
        reason.startswith(INSUFFICIENT_EVIDENCE_VERDICT_PREFIX)
        for reason in result.failure_reasons
    )
    # No costlier rung can supply tests the request did not name.
    assert result.no_rung_can_satisfy is True


def test_gate_refuses_an_untested_test_answer_as_insufficient() -> None:
    result = _gate("test", TEST_ANSWER, None)
    assert result.passed is False
    assert result.no_rung_can_satisfy is True


def test_gate_accepts_correct_code_whose_tests_passed_without_any_keyword() -> None:
    result = _gate("code_generation", CORRECT, _PASSED_RUN)
    assert result.passed is True
    assert result.fail_category == "pass"
    assert result.score_source == SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE
    assert result.score_verified or "names_resolve" in result.skipped_checks


@pytest.mark.parametrize("answer", [STUB, STUB_TYPING, WRONG])
def test_gate_refuses_code_whose_tests_failed_and_lets_the_ladder_climb(
    answer: str,
) -> None:
    result = _gate("code_generation", answer, _FAILED_RUN)
    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert any("passes_existing_tests" in reason for reason in result.failure_reasons)
    assert result.no_rung_can_satisfy is False


def test_a_test_class_answer_shaped_as_one_word_needs_no_tests() -> None:
    """A declared one-word reply is not a code answer; its shape override holds."""
    deterministic, _ = resolve_task_class_dod_checks(
        "test", prompt="Reply with exactly the word READY"
    )
    assert "passes_existing_tests" not in deterministic


# -- the shared acceptance, local and bus --------------------------------------


def _result(
    *,
    passed: bool = True,
    category: EnumQualityGateCategory = "pass",
    score: float,
    evaluations: tuple[ModelQualityRuleEvaluation, ...] = (),
    skipped: tuple[str, ...] = (),
) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=uuid4(),
        passed=passed,
        fail_category=category,
        quality_score=score,
        score_source=SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
        rule_evaluations=evaluations,
        skipped_checks=skipped,
    )


def _tests_evaluation(passed: bool) -> ModelQualityRuleEvaluation:
    return ModelQualityRuleEvaluation(
        rule="passes_existing_tests", enforcement="blocking", passed=passed
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        # Tests ran and passed: accepted whatever the keyword-laden score says.
        (_result(score=0.6, evaluations=(_tests_evaluation(True),)), True),
        (_result(score=0.733, evaluations=(_tests_evaluation(True),)), True),
        # A perfect score with the tests skipped: an older gate's verdict.
        (_result(score=1.0, skipped=("passes_existing_tests",)), False),
        # A perfect score with no test evaluation at all.
        (_result(score=1.0), False),
        (
            _result(
                passed=False,
                category="fail_deterministic",
                score=0.5,
                evaluations=(_tests_evaluation(False),),
            ),
            False,
        ),
        (
            _result(
                passed=False,
                category="fail_heuristic",
                score=0.9,
                evaluations=(_tests_evaluation(True),),
            ),
            False,
        ),
    ],
    ids=[
        "tests-passed-0.6",
        "tests-passed-0.733",
        "tests-skipped-1.0",
        "no-tests-1.0",
        "tests-failed",
        "gate-refused",
    ],
)
def test_bus_and_local_paths_give_the_same_code_class_verdict(
    monkeypatch: pytest.MonkeyPatch, result: ModelQualityGateResult, expected: bool
) -> None:
    _no_escalation(monkeypatch)
    assert _local_accepts("code_generation", result) is expected
    assert _bus_accepts("code_generation", result) is expected


@pytest.mark.parametrize("task_type", ["document", "documentation"])
def test_a_documentation_answer_still_needs_its_bar(
    monkeypatch: pytest.MonkeyPatch, task_type: str
) -> None:
    """AC3: a rubric-scored class keeps its bar on both paths."""
    _no_escalation(monkeypatch)
    bar = resolve_required_bar_authority(task_type=task_type).required_bar
    below = _result(score=round(bar - 0.05, 3))
    at_bar = _result(score=bar)
    assert _local_accepts(task_type, below) is False
    assert _local_accepts(task_type, at_bar) is True
    handler, intent = _bus_workflow(task_type=task_type, content="Prose answer.")
    below = below.model_copy(update={"correlation_id": intent.payload.correlation_id})
    events = handler.handle_gate_result(below)
    assert not any(isinstance(event, ModelDelegationCompleted) for event in events)
    handler, intent = _bus_workflow(task_type=task_type, content="Prose answer.")
    at_bar = at_bar.model_copy(update={"correlation_id": intent.payload.correlation_id})
    events = handler.handle_gate_result(at_bar)
    assert any(isinstance(event, ModelDelegationCompleted) for event in events)


@pytest.mark.parametrize("answer", [CORRECT, STUB, STUB_TYPING])
def test_bus_path_refuses_untested_code_and_does_not_climb(
    monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    _no_escalation(monkeypatch)
    handler, intent = _bus_workflow(task_type="code_generation", content=answer)
    gate_result = delta(intent.payload)
    assert gate_result.no_rung_can_satisfy is True
    events = handler.handle_gate_result(gate_result)
    assert not any(isinstance(event, ModelDelegationCompleted) for event in events)
    assert not any(isinstance(event, ModelRoutingIntent) for event in events)
    assert any(isinstance(event, ModelDelegationFailed) for event in events)


# -- a caller response contract is the sole acceptance authority ---------------

_JSON_CONTRACT: dict[str, object] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
_JSON_ANSWER = '{"answer": "def add(a, b):\\n    return a + b"}'


def test_bus_path_lets_a_response_contract_decide_a_code_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-21077: the schema replaces the class DoD (OMN-15193), tests included."""
    _no_escalation(monkeypatch)
    handler, intent = _bus_workflow(
        task_type="code_generation",
        content=_JSON_ANSWER,
        response_contract=_JSON_CONTRACT,
    )
    gate_result = delta(
        intent.payload, response_contract=intent.payload.response_contract
    )
    assert gate_result.passed is True, gate_result.failure_reasons
    events = handler.handle_gate_result(gate_result)
    assert any(isinstance(event, ModelDelegationCompleted) for event in events)


def test_local_path_lets_a_response_contract_decide_a_code_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-21077: same verdict on the local path, with no named tests."""
    _no_escalation(monkeypatch)
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning(_JSON_ANSWER),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = asyncio.run(
        port.dispatch(
            prompt=PROMPT,
            task_type="code_generation",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
            response_contract=_JSON_CONTRACT,
        )
    )
    assert result["status"] == "completed", result
    assert result["quality_gate_passed"] is True


# -- the named test spec ------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"answer_path": "../add_impl.py"},
        {"answer_path": "/abs/add_impl.py"},
        {"workspace_root": "relative/ws"},
        {"test_ids": ("../t.py::x",)},
        {"test_ids": ("--rootdir=/",)},
    ],
)
def test_named_test_spec_refuses_paths_outside_the_workspace(
    workspace: Path, bad: dict[str, object]
) -> None:
    fields: dict[str, object] = {
        "workspace_root": str(workspace),
        "answer_path": "add_impl.py",
        "test_ids": (_TEST_ID,),
        **bad,
    }
    with pytest.raises(ValueError, match=r"workspace|node id"):
        ModelNamedTestSpec.model_validate(fields)


# -- the local path end to end -------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "accepted"),
    [(CORRECT, True), (STUB, False), (STUB_TYPING, False), (WRONG, False)],
    ids=["correct", "stub", "stub-typing", "wrong"],
)
def test_local_path_accepts_only_code_whose_named_tests_pass(
    tmp_path: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    accepted: bool,
) -> None:
    _no_escalation(monkeypatch)
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning(_fenced(answer)),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        named_test_runner=_pytest_runner,
    )
    result = _dispatch(port, task_type="code_generation", named_tests=_spec(workspace))
    assert result["quality_gate_passed"] is accepted
    assert result["status"] == ("completed" if accepted else "failed")


def test_local_path_refuses_correct_code_when_no_runner_runs_the_named_tests(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests named but nothing wired to run them: never accepted untested."""
    _no_escalation(monkeypatch)
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning(_fenced(CORRECT)),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="code_generation", named_tests=_spec(workspace))
    assert result["quality_gate_passed"] is False
    assert result["status"] == "failed"


def test_local_path_refuses_correct_code_when_no_tests_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_escalation(monkeypatch)
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning(_fenced(CORRECT)),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="code_generation", named_tests=None)
    assert result["quality_gate_passed"] is False
    assert result["status"] == "failed"


# -- naming tests on a request -------------------------------------------------


def test_named_tests_are_read_from_request_metadata(workspace: Path) -> None:
    metadata = {
        NAMED_TESTS_METADATA_KEY: f"{_TEST_ID} test_add.py",
        NAMED_TESTS_ANSWER_PATH_METADATA_KEY: "add_impl.py",
        NAMED_TESTS_WORKSPACE_METADATA_KEY: str(workspace),
    }
    spec = ModelNamedTestSpec.from_metadata(metadata)
    assert spec is not None
    assert spec.test_ids == (_TEST_ID, "test_add.py")
    assert ModelNamedTestSpec.from_metadata({}) is None
    with pytest.raises(ValueError, match=NAMED_TESTS_ANSWER_PATH_METADATA_KEY):
        ModelNamedTestSpec.from_metadata({NAMED_TESTS_METADATA_KEY: _TEST_ID})


def _request(workspace: Path) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt=PROMPT,
        task_type="code_generation",
        source="codex",
        working_directory=str(workspace),
        metadata={
            NAMED_TESTS_METADATA_KEY: _TEST_ID,
            NAMED_TESTS_ANSWER_PATH_METADATA_KEY: "add_impl.py",
        },
    )


def test_named_tests_reach_a_port_that_runs_them(workspace: Path) -> None:
    async def dispatch(
        *, named_tests: ModelNamedTestSpec | None = None
    ) -> dict[str, object]:
        return {}

    kwargs = skill_mod._named_tests_dispatch_kwargs(_request(workspace), dispatch)
    assert kwargs == {"named_tests": _spec(workspace)}
    assert (
        "named_tests"
        in inspect.signature(LocalDelegationDispatchPort.dispatch).parameters
    )


def test_named_tests_are_withheld_from_a_port_that_cannot_run_them(
    workspace: Path,
) -> None:
    async def dispatch(*, prompt: str = "") -> dict[str, object]:
        return {}

    assert skill_mod._named_tests_dispatch_kwargs(_request(workspace), dispatch) == {}


def test_the_runtime_port_refuses_named_tests_it_cannot_run(workspace: Path) -> None:
    """A deployed lane has no access to the caller's workspace (OMN-21077)."""
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
        RuntimeDelegationDispatchPort,
    )

    port = RuntimeDelegationDispatchPort.__new__(RuntimeDelegationDispatchPort)
    with pytest.raises(ValueError, match="in-process path"):
        asyncio.run(
            port.dispatch(
                prompt=PROMPT,
                task_type="code_generation",
                correlation_id=uuid4(),
                max_tokens=256,
                source_file_path=None,
                source_session_id=None,
                wait=True,
                execution_timeout_seconds=240,
                terminal_delivery_margin_seconds=60,
                quality_contract_mode="extend_task_class",
                acceptance_criteria=(),
                tenant_id=None,
                named_tests=_spec(workspace),
            )
        )
