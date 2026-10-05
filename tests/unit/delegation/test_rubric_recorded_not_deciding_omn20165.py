# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20165: rubric evidence is recorded after, and never decides, each gate."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.delegation.rubric import attempt_verdict
from omnimarket.enums.enum_delegation_acceptance import EnumDelegationAcceptanceDecision
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _attempt_records,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as local_module,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as workflow_module,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.routing import delegation_backend_resolution
from omnimarket.routing.model_escalation_decision_result import (
    ModelEscalationDecisionResult,
)
from tests.unit.delegation.test_local_port_response_contract_evidence_omn19201 import (
    _FIELDS_CONTRACT,
    _backends,
    _RecordingEffect,
)
from tests.unit.delegation.test_omn19436_attempts_record_finish_reason import (
    _no_further_rung,
    _request,
    _routing,
)

pytestmark = pytest.mark.unit

_DIFF = (
    "Review this synthetic diff:\n"
    "diff --git a/src/widget.py b/src/widget.py\n"
    "--- a/src/widget.py\n+++ b/src/widget.py\n"
    "@@ -1 +1 @@\n-widget_value = 1\n+widget_value = 2\n"
)
_BAD_CITATION = "src/absent.py:1 has a defect."
_GOOD_CITATION = "src/widget.py:1 `widget_value = 2`"


def _ready(
    *, task_class: str = "code_review", answer: str = _BAD_CITATION
) -> tuple[HandlerDelegationWorkflow, UUID]:
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(
        _request(cid).model_copy(update={"task_type": task_class, "prompt": _DIFF})
    )
    _answer(handler, cid, task_class=task_class, answer=answer, tier="local")
    return handler, cid


def _answer(
    handler: HandlerDelegationWorkflow,
    cid: UUID,
    *,
    task_class: str = "code_review",
    answer: str = _BAD_CITATION,
    tier: str = "cheap_cloud",
) -> None:
    handler.handle_routing_decision(
        _routing(cid, tier).model_copy(
            update={"task_type": task_class, "selected_model": "synthetic-model"}
        )
    )
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content=f"### ANSWER\n{answer}",
            model_used="synthetic-model",
            latency_ms=1,
        )
    )


def _gate(cid: UUID, passed: bool) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=cid,
        passed=passed,
        quality_score=0.99 if passed else 0.2,
        failure_reasons=() if passed else ("synthetic_gate_rejection",),
        fallback_recommended=not passed,
    )


def _enable_one_climb(
    handler: HandlerDelegationWorkflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler, "_maybe_retry_local", lambda *_a, **_k: None)
    monkeypatch.setattr(
        handler,
        "_decide_escalation",
        lambda *_a, **_k: ModelEscalationDecisionResult(
            can_escalate=True, next_tier_name="cheap_cloud"
        ),
    )


async def _local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    accepted: bool = True,
) -> tuple[dict[str, object], _RecordingEffect]:
    # Reuse the existing effect and backend helper, replacing its endpoint with
    # a reserved synthetic URL. The effect is injected and performs no network I/O.
    backends = _backends()
    backends[0].update(
        endpoint_url="https://synthetic.invalid/v1/chat/completions",
        model_name="synthetic-model",
    )
    monkeypatch.setattr(
        delegation_backend_resolution, "load_bifrost_backends", lambda **_: backends
    )
    effect = _RecordingEffect(
        json.dumps({"fields": ["alpha", "beta", "gamma"] if accepted else ["alpha"]})
    )
    port = local_module.LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "synthetic.sqlite",
        effect_process_boundary=False,
    )
    result = await port.dispatch(
        prompt="List three synthetic fields.",
        task_type="document",
        correlation_id=uuid4(),
        max_tokens=None,
        source_file_path=None,
        source_session_id=None,
        wait=True,
        execution_timeout_seconds=60,
        terminal_delivery_margin_seconds=5,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id=None,
        backend_id="local-coder",
        response_contract=_FIELDS_CONTRACT,
    )
    return result, effect


def test_rubric_recorded_not_deciding_accepted_rung_carries_a_verdict() -> None:
    handler, cid = _ready()
    terminal = handler.handle_gate_result(_gate(cid, True))
    workflow = handler.workflows[cid]
    assert terminal
    assert workflow.state is EnumDelegationState.COMPLETED
    assert workflow.escalation_count == 0
    assert workflow.inference_content == _BAD_CITATION
    rung = workflow.escalation_history[0]
    assert rung.acceptance_decision is EnumDelegationAcceptanceDecision.ACCEPT
    assert rung.rubric_verdict is not None
    assert rung.rubric_verdict.outcome == "FAIL"
    assert "cited_lines_exist" in rung.rubric_verdict.failed_criteria
    mapped = _attempt_records({"escalation_history": [rung.model_dump(mode="json")]})
    assert mapped[0].rubric_verdict == rung.rubric_verdict


def test_rubric_recorded_not_deciding_rejected_rung_still_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, cid = _ready(answer=_GOOD_CITATION)
    _enable_one_climb(handler, monkeypatch)
    events = handler.handle_gate_result(_gate(cid, False))
    workflow = handler.workflows[cid]
    assert workflow.state is EnumDelegationState.ROUTED
    assert workflow.escalation_count == 1
    assert events[0].min_tier_name == "cheap_cloud"
    rung = workflow.escalation_history[0]
    assert rung.acceptance_decision is EnumDelegationAcceptanceDecision.CLIMB
    assert rung.rubric_verdict is not None
    assert rung.rubric_verdict.outcome == "PASS"
    _answer(handler, cid)
    handler.handle_gate_result(_gate(cid, True))
    assert workflow.state is EnumDelegationState.COMPLETED
    assert workflow.escalation_count == 1
    assert len(workflow.escalation_history) == 2
    assert all(rung.rubric_verdict is not None for rung in workflow.escalation_history)


@pytest.mark.parametrize(
    ("first_passed", "last_passed"), [(True, True), (False, True), (False, False)]
)
async def test_rubric_recorded_not_deciding_decision_identical_with_rubric_forced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_passed: bool,
    last_passed: bool,
) -> None:
    snapshots: list[bytes] = []
    for forced in ("PASS", "FAIL", "raise"):

        def record(
            *,
            task_class: str,
            request_text: str,
            answer_text: str,
            forced: str = forced,
        ) -> ModelAttemptRubricVerdict:
            if forced == "raise":
                raise RuntimeError("synthetic recording fault")
            return ModelAttemptRubricVerdict(
                rubric_version="synthetic.v1",
                task_class=task_class,
                outcome=forced,
                failed_criteria=("cited_lines_exist",) if forced == "FAIL" else (),
            )

        for module in (attempt_verdict, workflow_module, local_module):
            monkeypatch.setattr(module, "record_attempt_rubric_verdict", record)
        handler, cid = _ready()
        _enable_one_climb(handler, monkeypatch)
        handler.handle_gate_result(_gate(cid, first_passed))
        if not first_passed:
            _answer(handler, cid)
            monkeypatch.setattr(handler, "_decide_escalation", _no_further_rung)
            handler.handle_gate_result(_gate(cid, last_passed))
        workflow = handler.workflows[cid]
        local, _ = await _local(tmp_path, monkeypatch, accepted=first_passed)
        local_attempts = _attempt_records(local)
        assert all(
            rung.rubric_verdict is not None for rung in workflow.escalation_history
        )
        assert all(rung.rubric_verdict is not None for rung in local_attempts)
        snapshots.append(
            json.dumps(
                {
                    "bus": {
                        "status": workflow.state.value,
                        "escalation_count": workflow.escalation_count,
                        "decisions": [
                            (r.acceptance_decision.value, r.acceptance_reason.value)
                            for r in workflow.escalation_history
                        ],
                    },
                    "local": {
                        "status": local["status"],
                        "escalation_count": local["escalation_count"],
                        "decisions": [
                            (r.acceptance_decision.value, r.acceptance_reason.value)
                            for r in local_attempts
                        ],
                    },
                },
                sort_keys=True,
            ).encode()
        )
    assert snapshots[0] == snapshots[1] == snapshots[2]


async def test_rubric_recorded_not_deciding_local_port_attempt_carries_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    original = local_module.record_attempt_rubric_verdict

    def record(**kwargs: Any) -> ModelAttemptRubricVerdict:
        calls.append((kwargs["request_text"], kwargs["answer_text"]))
        return original(**kwargs)

    monkeypatch.setattr(local_module, "record_attempt_rubric_verdict", record)
    result, effect = await _local(tmp_path, monkeypatch)
    assert result["status"] == "completed"
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    verdict = attempts[0]["rubric_verdict"]
    assert isinstance(verdict, dict)
    assert _attempt_records(result)[
        0
    ].rubric_verdict == ModelAttemptRubricVerdict.model_validate(verdict)
    assert calls == [(effect.calls[0].prompt, effect.content)]


@pytest.mark.parametrize("key", ["attempts", "escalation_history"])
@pytest.mark.parametrize("raw", [{"outcome": "invalid"}, None, "invalid"])
def test_rubric_recorded_not_deciding_mapper_tolerates_malformed(
    key: str, raw: object
) -> None:
    records = _attempt_records({key: [{"rubric_verdict": raw}]})
    assert len(records) == 1
    assert records[0].rubric_verdict is None


def test_rubric_recorded_not_deciding_class_without_rubric_is_undetermined() -> None:
    # OMN-20552 gave document a rubric; planning still declares none.
    handler, cid = _ready(task_class="planning", answer="Synthetic plan.")
    handler.handle_gate_result(_gate(cid, True))
    workflow = handler.workflows[cid]
    assert workflow.state is EnumDelegationState.COMPLETED
    verdict = workflow.escalation_history[0].rubric_verdict
    assert verdict is not None
    assert verdict.outcome == "UNDETERMINED"
    assert verdict.undetermined_criteria == ("no_rubric_for_class",)


def test_rubric_recorded_not_deciding_compute_error_is_undetermined(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    answer = "SYNTHETIC_PRIVATE_ANSWER_TOKEN"

    def fail(*_args: object) -> None:
        raise RuntimeError(answer)

    monkeypatch.setattr(attempt_verdict.HandlerDelegationRubricCheck, "handle", fail)
    with caplog.at_level(logging.WARNING):
        verdict = attempt_verdict.record_attempt_rubric_verdict(
            task_class="code_review", request_text=_DIFF, answer_text=answer
        )
    assert verdict.outcome == "UNDETERMINED"
    assert verdict.undetermined_criteria == ("rubric_check_error",)
    assert verdict.rubric_version == attempt_verdict._load_contract().rubric_version
    assert caplog.messages == ["Rubric check failed: RuntimeError"]
    assert answer not in caplog.text
    assert _DIFF not in caplog.text


@pytest.mark.parametrize("task_class", ["", "code_review"])
def test_rubric_recorded_not_deciding_load_and_request_errors_are_undetermined(
    monkeypatch: pytest.MonkeyPatch, task_class: str
) -> None:
    expected_version = attempt_verdict._load_contract().rubric_version
    if task_class:

        def fail() -> None:
            raise ValueError("synthetic contract fault")

        monkeypatch.setattr(attempt_verdict, "_load_contract", fail)
        expected_version = "unavailable"
    verdict = attempt_verdict.record_attempt_rubric_verdict(
        task_class=task_class, request_text=_DIFF, answer_text=_BAD_CITATION
    )
    assert verdict.task_class == (task_class or "unknown")
    assert verdict.rubric_version == expected_version
    assert verdict.outcome == "UNDETERMINED"
    assert verdict.undetermined_criteria == ("rubric_check_error",)
