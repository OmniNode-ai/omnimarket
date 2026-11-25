# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A response contract adds to a code class's checks and never replaces them (OMN-26656).

A caller-supplied response contract used to empty the check set of a code task
class: the gate returned the schema verdict alone, and both acceptance sites
judged the answer against an empty declared set, so a code request carrying any
schema and a failing named test was accepted. These tests pin the rule on the
gate, the bus-less port and the bus workflow:

- a code class is checked on its executed tests AND the contract;
- a class not accepted by executed tests is decided by the contract as before;
- a code answer's rule evaluations name the class check and the contract check.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelDelegationNamedTests

from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_named_test_run_request import (
    EnumNamedTestRunStatus,
    ModelNamedTestFile,
    ModelNamedTestRunRequest,
    ModelNamedTestRunResult,
    ModelNamedTestSpec,
)
from omnimarket.models.delegation.wire.model_quality_gate import (
    NO_NAMED_TEST_RUN_REASON,
    NO_NAMED_TESTS_UNTESTED_REASON,
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
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
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
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

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("stub_provider_quota_reader"),
]

PROMPT = "add two ints"
_TEST_ID = "test_add.py::test_add"
_TEST_SOURCE = (
    "from add_impl import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
)
# Any schema: the point is that the contract passes while the tests decide.
_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"code": {"type": "string"}},
    "required": ["code"],
}
_ANSWER = json.dumps({"code": "def add(a, b):\n    return a + b"})
_FAILED_RUN = ModelNamedTestRun(
    test_ids=(_TEST_ID,), failed_test_ids=(_TEST_ID,), exit_code=1
)
_PASSED_RUN = ModelNamedTestRun(test_ids=(_TEST_ID,), exit_code=0)
_LOCAL_BACKEND = ModelResolvedDelegationBackend(
    backend_id="local-coder",
    model_id="fixture-local-model",
    endpoint_ref="https://local.example/v1/chat/completions",
    tier="local",
    max_tokens=4096,
    timeout_ms=30000,
)


def _rules(result: ModelQualityGateResult) -> dict[str, bool]:
    return {
        evaluation.rule: evaluation.passed for evaluation in result.rule_evaluations
    }


def _gate(
    task_type: str,
    *,
    run: ModelNamedTestRun | None = None,
    requested: bool = False,
    content: str = _ANSWER,
) -> ModelQualityGateResult:
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, prompt=PROMPT)
    return delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=task_type,
            llm_response_content=content,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        response_contract=_SCHEMA,
        named_test_run=run,
        named_tests_requested=requested,
    )


# -- the gate ------------------------------------------------------------------


def test_gate_refuses_a_code_answer_with_a_failing_named_test_under_a_contract() -> (
    None
):
    result = _gate("code_generation", run=_FAILED_RUN, requested=True)
    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert any("passes_existing_tests" in r for r in result.failure_reasons)


def test_gate_accepts_a_code_answer_whose_named_test_passes_under_a_contract() -> None:
    result = _gate("code_generation", run=_PASSED_RUN, requested=True)
    assert result.passed is True
    assert result.fail_category == "pass"


def test_gate_refuses_named_tests_that_never_ran_under_a_contract() -> None:
    result = _gate("code_generation", run=None, requested=True)
    assert result.passed is False
    assert NO_NAMED_TEST_RUN_REASON in result.failure_reasons


def test_gate_still_refuses_a_schema_miss_on_a_code_class() -> None:
    result = _gate("code_generation", run=_PASSED_RUN, requested=True, content="{}")
    assert result.passed is False


def test_gate_records_a_code_answer_with_no_tests_named_untested_under_a_contract() -> (
    None
):
    result = _gate("code_generation")
    assert result.passed is True
    tests = [e for e in result.rule_evaluations if e.rule == "passes_existing_tests"]
    assert [e.detail for e in tests] == [NO_NAMED_TESTS_UNTESTED_REASON]


def test_gate_receipt_names_the_class_check_and_the_contract_check() -> None:
    result = _gate("code_generation", run=_PASSED_RUN, requested=True)
    rules = _rules(result)
    assert rules["passes_existing_tests"] is True
    assert rules["response_contract"] is True


def test_gate_receipt_names_a_failing_class_check_beside_a_passing_contract() -> None:
    result = _gate("code_generation", run=_FAILED_RUN, requested=True)
    rules = _rules(result)
    assert rules["passes_existing_tests"] is False
    assert rules["response_contract"] is True


def test_gate_decides_a_class_not_accepted_by_executed_tests_by_the_contract() -> None:
    """AC2: a document request's verdict is the contract's, as it was."""
    result = _gate("document", run=_FAILED_RUN, requested=True)
    assert result.passed is True
    assert result.fail_category == "pass"
    assert result.rule_evaluations == ()
    assert result.failure_reasons == ()


def test_gate_refuses_a_document_answer_that_misses_its_contract() -> None:
    result = _gate("document", content="{}")
    assert result.passed is False


# -- the bus-less port ---------------------------------------------------------


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


def _runner(run: ModelNamedTestRun) -> port_mod._NamedTestRunner:
    def _run(_request: ModelNamedTestRunRequest) -> ModelNamedTestRunResult:
        return ModelNamedTestRunResult(status=EnumNamedTestRunStatus.COMPLETED, run=run)

    return _run


def _local(
    tmp_path: Path,
    *,
    task_type: str,
    run: ModelNamedTestRun,
    named: bool = True,
) -> dict[str, object]:
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning(_ANSWER),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        named_test_runner=_runner(run),
    )
    spec = (
        ModelNamedTestSpec(
            answer_path="add_impl.py",
            test_ids=(_TEST_ID,),
            test_files=(ModelNamedTestFile(path="test_add.py", content=_TEST_SOURCE),),
        )
        if named
        else None
    )
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
            response_contract=_SCHEMA,
            named_tests=spec,
        )
    )


def test_local_path_refuses_a_code_answer_with_a_failing_named_test_under_a_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_escalation(monkeypatch)
    result = _local(tmp_path, task_type="code_generation", run=_FAILED_RUN)
    assert result["quality_gate_passed"] is False
    assert result["status"] == "failed"


def test_local_path_accepts_a_code_answer_whose_named_test_passes_under_a_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_escalation(monkeypatch)
    result = _local(tmp_path, task_type="code_generation", run=_PASSED_RUN)
    assert result["quality_gate_passed"] is True
    assert result["status"] == "completed"


def test_local_path_decides_a_document_answer_by_its_contract_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_escalation(monkeypatch)
    result = _local(tmp_path, task_type="document", run=_FAILED_RUN)
    assert result["quality_gate_passed"] is True
    assert result["status"] == "completed"


# -- the bus workflow ----------------------------------------------------------


def _routing(cid: UUID, task_type: str) -> ModelRoutingDecision:
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, prompt=PROMPT)
    return ModelRoutingDecision(
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
        rationale="Controlled single-tier route.",
        tier_name="local",
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
    )


def _named_tests() -> ModelDelegationNamedTests:
    return ModelDelegationNamedTests.model_validate(
        {
            "test_ids": [_TEST_ID],
            "answer_path": "add_impl.py",
            "test_files": [{"path": "test_add.py", "content": _TEST_SOURCE}],
        }
    )


def _bus_runner(run: ModelNamedTestRun) -> object:
    def _run(_request: ModelNamedTestRunRequest) -> ModelNamedTestRunResult:
        return ModelNamedTestRunResult(status=EnumNamedTestRunStatus.COMPLETED, run=run)

    return _run


async def _bus_terminal(
    monkeypatch: pytest.MonkeyPatch,
    *,
    task_type: str,
    run: ModelNamedTestRun,
) -> tuple[ModelQualityGateResult, bool]:
    _no_escalation(monkeypatch)
    handler = hw.HandlerDelegationWorkflow(
        workflows={},
        named_test_runner=cast(Any, _bus_runner(run)),
    )
    cid = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt=PROMPT,
            task_type=task_type,
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
            response_contract=_SCHEMA,
            named_tests=_named_tests(),
        )
    )
    handler.handle_routing_decision(_routing(cid, task_type))
    events = await handler.handle(
        ModelInferenceResponseData(
            correlation_id=cid,
            content=_ANSWER,
            model_used="fixture-model",
            latency_ms=10,
        )
    )
    intent = next(e for e in events if isinstance(e, ModelQualityGateIntent))
    verdict = HandlerQualityGateIntent().handle(intent)
    terminal = handler.handle_gate_result(verdict)
    return verdict, any(isinstance(e, ModelDelegationCompleted) for e in terminal)


@pytest.mark.asyncio
async def test_bus_path_refuses_a_code_answer_with_a_failing_named_test_under_a_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict, completed = await _bus_terminal(
        monkeypatch, task_type="code_generation", run=_FAILED_RUN
    )
    assert verdict.passed is False
    assert completed is False


@pytest.mark.asyncio
async def test_bus_path_accepts_a_code_answer_whose_named_test_passes_under_a_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict, completed = await _bus_terminal(
        monkeypatch, task_type="code_generation", run=_PASSED_RUN
    )
    assert verdict.passed is True
    assert completed is True
    assert _rules(verdict)["passes_existing_tests"] is True
    assert _rules(verdict)["response_contract"] is True


@pytest.mark.asyncio
async def test_bus_path_decides_a_document_answer_by_its_contract_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict, completed = await _bus_terminal(
        monkeypatch, task_type="document", run=_FAILED_RUN
    )
    assert verdict.passed is True
    assert completed is True


# -- the two acceptance sites -------------------------------------------------


def _a_gate_pass_with_no_executed_check_evaluation(cid: UUID) -> ModelQualityGateResult:
    """A pass from a gate that never evaluated the class's test check."""
    return ModelQualityGateResult(
        correlation_id=cid,
        passed=True,
        fail_category="pass",
        quality_score=1.0,
        failure_reasons=(),
        fallback_recommended=False,
    )


@pytest.mark.asyncio
async def test_bus_site_refuses_a_code_pass_that_never_evaluated_its_tests_under_a_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_escalation(monkeypatch)
    handler = hw.HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt=PROMPT,
            task_type="code_generation",
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
            response_contract=_SCHEMA,
        )
    )
    handler.handle_routing_decision(_routing(cid, "code_generation"))
    await handler.handle(
        ModelInferenceResponseData(
            correlation_id=cid,
            content=_ANSWER,
            model_used="fixture-model",
            latency_ms=10,
        )
    )
    terminal = handler.handle_gate_result(
        _a_gate_pass_with_no_executed_check_evaluation(cid)
    )
    assert not any(isinstance(e, ModelDelegationCompleted) for e in terminal)
    assert handler.workflows[cid].gate_result is not None
    gate_result = handler.workflows[cid].gate_result
    assert gate_result is not None
    assert NO_NAMED_TEST_RUN_REASON in gate_result.failure_reasons


def test_checks_in_force_keep_the_executed_check_of_a_code_class_under_a_contract() -> (
    None
):
    from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
        checks_in_force_under_response_contract,
        resolve_required_bar_authority,
    )

    code = resolve_required_bar_authority(task_type="code_generation")
    document = resolve_required_bar_authority(task_type="document")
    class_checks = ("compiles_without_errors", "passes_existing_tests")
    assert checks_in_force_under_response_contract(
        class_checks, code, response_contract_governed=True
    ) == ("passes_existing_tests",)
    assert (
        checks_in_force_under_response_contract(
            class_checks, code, response_contract_governed=False
        )
        == class_checks
    )
    assert (
        checks_in_force_under_response_contract(
            class_checks, document, response_contract_governed=True
        )
        == ()
    )


# -- the 7-day count (AC4) -----------------------------------------------------


def _run_dir(
    root: Path, name: str, task: str, status: str, evidence: bool, age_days: int
) -> None:
    import os
    import time

    d = root / ".onex_state" / "runs" / name
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"task_type": task}))
    receipt: dict[str, Any] = {"status": status}
    if evidence:
        receipt["response_contract_evidence"] = {"schema": "x"}
    (d / "receipt.json").write_text(json.dumps(receipt))
    old = time.time() - age_days * 86400
    os.utime(d / "receipt.json", (old, old))


def test_seven_day_count_counts_only_recent_accepted_contract_governed_code(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_delegation_orchestrator.governed_code_count import (
        count_governed_code_answers,
    )

    _run_dir(tmp_path, "a", "code_generation", "success", True, 1)
    _run_dir(tmp_path, "b", "code_generation", "success", False, 1)
    _run_dir(tmp_path, "c", "document", "success", True, 1)
    _run_dir(tmp_path, "d", "code_generation", "failed", True, 1)
    _run_dir(tmp_path, "e", "code_generation", "success", True, 30)
    assert count_governed_code_answers(tmp_path / ".onex_state" / "runs") == 1
