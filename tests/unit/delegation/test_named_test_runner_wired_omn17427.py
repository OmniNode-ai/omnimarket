# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A code-class gate receives a real test run, and no tests named is UNTESTED (OMN-17427).

On the dev lane every code_generation and test delegation was refused as
``INSUFFICIENT: no named test run reached the gate``, including answers whose
tests pass, because no named-test runner was passed into the delegation port
anywhere and a request could not carry its tests. These tests pin the fix:

- the in-process port runs HandlerNamedTestRun by default, in a sandbox,
  on tests the request carries inline (no caller workspace needed);
- a passing answer is accepted and a failing one rejected, end to end;
- a code-class request that names no tests is decided UNTESTED by its contract
  checks, never refused as INSUFFICIENT and never recorded as tested;
- a request that named tests no runner ran is still refused as INSUFFICIENT.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_answer_gate_request import (
    EnumAnswerGateKind,
    EnumNamedTestVerdict,
    ModelAnswerGateRequest,
)
from omnimarket.models.delegation.wire.model_named_test_run_request import (
    NAMED_TESTS_ANSWER_PATH_METADATA_KEY,
    NAMED_TESTS_FILES_METADATA_KEY,
    NAMED_TESTS_METADATA_KEY,
    ModelNamedTestFile,
    ModelNamedTestSpec,
)
from omnimarket.models.delegation.wire.model_quality_gate import (
    INSUFFICIENT_EVIDENCE_VERDICT_PREFIX,
    NO_NAMED_TEST_RUN_REASON,
    NO_NAMED_TESTS_UNTESTED_REASON,
    UNTESTED_VERDICT_PREFIX,
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_answer_gate_compute.handlers.handler_delegation_answer_gate import (
    HandlerDelegationAnswerGate,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hw,
)
from omnimarket.nodes.node_delegation_orchestrator.models import (
    ModelDelegationCompleted,
    ModelDelegationRequest,
    ModelInferenceResponseData,
    ModelQualityGateIntent,
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

pytestmark = pytest.mark.unit

PROMPT = "add two ints"
CORRECT = "def add(a, b):\n    return a + b"
WRONG = "def add(a, b):\n    return a - b"
_TEST_ID = "test_add.py::test_add"
_TEST_SOURCE = (
    "from add_impl import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
)
_LOCAL_BACKEND = ModelResolvedDelegationBackend(
    backend_id="local-coder",
    model_id="Qwen3.8-27B",
    endpoint_ref="https://local.example/v1/chat/completions",
    tier="local",
    max_tokens=4096,
    timeout_ms=30000,
)


def _inline_spec() -> ModelNamedTestSpec:
    return ModelNamedTestSpec(
        answer_path="add_impl.py",
        test_ids=(_TEST_ID,),
        test_files=(ModelNamedTestFile(path="test_add.py", content=_TEST_SOURCE),),
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
    def _effect(request: ModelLlmDelegationCallRequest) -> ModelLlmDelegationCallResult:
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
    port: LocalDelegationDispatchPort, named_tests: ModelNamedTestSpec | None
) -> dict[str, object]:
    return asyncio.run(
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
            named_tests=named_tests,
        )
    )


def _default_port(tmp_path: Path, answer: str) -> LocalDelegationDispatchPort:
    # No named_test_runner: the port's own default, the sandboxed runner node.
    return LocalDelegationDispatchPort(
        effect_handler=_effect_returning(_fenced(answer)),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )


def _gate(content: str, *, named_tests_requested: bool) -> ModelQualityGateResult:
    deterministic, heuristic = resolve_task_class_dod_checks(
        "code_generation", prompt=PROMPT
    )
    return delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="code_generation",
            llm_response_content=content,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        named_tests_requested=named_tests_requested,
    )


# -- golden chain: the request carries its tests, the port runs them ----------


def test_golden_chain_accepts_a_passing_answer_on_its_inline_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_escalation(monkeypatch)
    result = _dispatch(_default_port(tmp_path, CORRECT), _inline_spec())
    assert result["quality_gate_passed"] is True
    assert result["status"] == "completed"


def test_golden_chain_rejects_a_failing_answer_on_its_inline_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_escalation(monkeypatch)
    result = _dispatch(_default_port(tmp_path, WRONG), _inline_spec())
    assert result["quality_gate_passed"] is False
    assert result["status"] == "failed"
    assert INSUFFICIENT_EVIDENCE_VERDICT_PREFIX not in json.dumps(result, default=str)


def test_golden_chain_decides_a_code_answer_with_no_tests_named_untested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_escalation(monkeypatch)
    result = _dispatch(_default_port(tmp_path, CORRECT), None)
    assert result["quality_gate_passed"] is True
    assert result["status"] == "completed"


# -- the reducer: UNTESTED vs INSUFFICIENT ------------------------------------


def test_reducer_records_no_tests_named_as_untested_not_insufficient() -> None:
    result = _gate(CORRECT, named_tests_requested=False)
    assert result.passed is True
    assert not any(
        reason.startswith(INSUFFICIENT_EVIDENCE_VERDICT_PREFIX)
        for reason in result.failure_reasons
    )
    assert "passes_existing_tests" in result.skipped_checks
    untested = [
        evaluation
        for evaluation in result.rule_evaluations
        if evaluation.rule == "passes_existing_tests"
    ]
    assert len(untested) == 1
    assert untested[0].passed is False
    assert untested[0].enforcement == "scored"
    assert untested[0].detail == NO_NAMED_TESTS_UNTESTED_REASON
    assert NO_NAMED_TESTS_UNTESTED_REASON.startswith(f"{UNTESTED_VERDICT_PREFIX}:")


def test_reducer_still_refuses_named_tests_that_never_ran() -> None:
    result = _gate(CORRECT, named_tests_requested=True)
    assert result.passed is False
    assert NO_NAMED_TEST_RUN_REASON in result.failure_reasons


def test_bus_path_completes_an_untested_code_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployed lane's gate carries no tests; its answer is UNTESTED, not refused."""
    _no_escalation(monkeypatch)
    handler = hw.HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt=PROMPT,
            task_type="code_generation",
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
        )
    )
    deterministic, heuristic = resolve_task_class_dod_checks(
        "code_generation", prompt=PROMPT
    )
    handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=cid,
            task_type="code_generation",
            selected_model="fixture-model",
            selected_backend_id=uuid4(),
            selected_backend_ref="fixture-backend",
            endpoint_url="http://provider.invalid:8000",
            cost_tier="low",
            max_context_tokens=65536,
            max_tokens=2048,
            system_prompt="Return the requested artifact.",
            rationale="Controlled single-tier route.",
            tier_name="local",
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        )
    )
    contract = handler.workflows[cid].effective_deliverable_contract
    assert contract is not None
    content = _fenced(CORRECT)
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
    intent = next(e for e in events if isinstance(e, ModelQualityGateIntent))
    gate_result = delta(intent.payload)
    assert gate_result.passed is True
    events = handler.handle_gate_result(gate_result)
    assert any(isinstance(event, ModelDelegationCompleted) for event in events)


# -- the answer gate's typed verdict -----------------------------------------


@pytest.mark.parametrize(
    ("requested", "verdict", "accepted"),
    [
        (False, EnumNamedTestVerdict.UNTESTED, True),
        (True, EnumNamedTestVerdict.NOT_RUN, False),
    ],
    ids=["none-named-untested", "named-not-run"],
)
def test_answer_gate_types_a_code_answer_with_no_test_run(
    requested: bool, verdict: EnumNamedTestVerdict, accepted: bool
) -> None:
    contract = ModelQualityGateResult(
        correlation_id=uuid4(),
        passed=True,
        fail_category="pass",
        quality_score=1.0,
    )
    gate = HandlerDelegationAnswerGate().handle(
        ModelAnswerGateRequest(
            correlation_id=uuid4(),
            task_type="code_generation",
            gate_kind=EnumAnswerGateKind.CODE,
            producer_model_id="Qwen3.8-27B",
            contract_gate=contract,
            named_tests_requested=requested,
        )
    )
    assert gate.tests_verdict is verdict
    assert gate.accepted is accepted
    assert gate.tests_passed is False


# -- the request carries its tests -------------------------------------------


def test_named_tests_files_metadata_needs_no_workspace() -> None:
    spec = ModelNamedTestSpec.from_metadata(
        {
            NAMED_TESTS_METADATA_KEY: _TEST_ID,
            NAMED_TESTS_ANSWER_PATH_METADATA_KEY: "add_impl.py",
            NAMED_TESTS_FILES_METADATA_KEY: json.dumps({"test_add.py": _TEST_SOURCE}),
        }
    )
    assert spec == _inline_spec()
    assert spec is not None
    assert spec.workspace_root is None


@pytest.mark.parametrize(
    "files",
    ["not json", json.dumps(["test_add.py"]), json.dumps({"../escape.py": "x"})],
    ids=["not-json", "not-an-object", "escapes-workspace"],
)
def test_named_tests_files_metadata_refuses_a_bad_value(files: str) -> None:
    with pytest.raises(ValueError, match=r"named_tests_files|workspace"):
        ModelNamedTestSpec.from_metadata(
            {
                NAMED_TESTS_METADATA_KEY: _TEST_ID,
                NAMED_TESTS_ANSWER_PATH_METADATA_KEY: "add_impl.py",
                NAMED_TESTS_FILES_METADATA_KEY: files,
            }
        )


def test_a_test_file_cannot_overwrite_the_answer() -> None:
    with pytest.raises(ValueError, match="answer_path"):
        ModelNamedTestSpec(
            answer_path="add_impl.py",
            test_ids=(_TEST_ID,),
            test_files=(ModelNamedTestFile(path="add_impl.py", content="x"),),
        )
