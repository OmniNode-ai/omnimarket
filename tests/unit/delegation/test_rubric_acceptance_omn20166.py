# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20166: only measured class rubrics may refuse and start the chain."""

import pytest
import yaml

from omnimarket.delegation.rubric import attempt_verdict
from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.models.ranges import EnumRangeVerdict
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.ranges.register import (
    DEFAULT_CHECK_REGISTER_PATH,
    validate_check_register,
)
from tests.unit.delegation.test_rubric_recorded_not_deciding_omn20165 import (
    _GOOD_CITATION,
    _answer,
    _enable_one_climb,
    _gate,
    _ready,
)

pytestmark = pytest.mark.unit


def _set_status(monkeypatch, task_class, status):
    contract = load_delegation_class_rubrics()
    raw = contract.model_dump(mode="json")
    raw["false_pass_status"][task_class] = status.value
    configured = type(contract).model_validate(raw)
    monkeypatch.setattr(attempt_verdict, "_load_contract", lambda: configured)


def test_rubric_not_met_recorded_only_shipped_config():
    contract = load_delegation_class_rubrics()
    assert set(contract.false_pass_status) == set(contract.classes)
    assert all(
        status is not EnumRangeVerdict.MET
        for status in contract.false_pass_status.values()
    )
    handler, cid = _ready()
    handler.handle_gate_result(_gate(cid, True))
    workflow = handler.workflows[cid]
    assert workflow.state is EnumDelegationState.COMPLETED
    assert workflow.escalation_history[0].rubric_verdict.outcome == "FAIL"


def test_rubric_not_met_recorded_only_other_class(monkeypatch):
    _set_status(monkeypatch, "summarization", EnumRangeVerdict.MET)
    handler, cid = _ready()
    handler.handle_gate_result(_gate(cid, True))
    assert handler.workflows[cid].state is EnumDelegationState.COMPLETED


def test_rubric_met_refuses_and_next_responder_can_succeed(monkeypatch):
    _set_status(monkeypatch, "code_review", EnumRangeVerdict.MET)
    handler, cid = _ready()
    _enable_one_climb(handler, monkeypatch)
    events = handler.handle_gate_result(_gate(cid, True))
    workflow = handler.workflows[cid]
    assert workflow.state is EnumDelegationState.ROUTED
    assert workflow.escalation_count == 1
    assert events[0].min_tier_name == "cheap_cloud"
    attempt = workflow.escalation_history[0]
    assert attempt.failure_class == "rubric_failed"
    assert any("cited_lines_exist" in reason for reason in attempt.failure_reasons)
    _answer(handler, cid, answer=_GOOD_CITATION)
    handler.handle_gate_result(_gate(cid, True))
    assert workflow.state is EnumDelegationState.COMPLETED
    assert workflow.escalation_history[-1].rubric_verdict.outcome == "PASS"


def test_rubric_failed_is_typed_failure(monkeypatch):
    assert (
        EnumDelegationFailureClass("rubric_failed")
        is EnumDelegationFailureClass.RUBRIC_FAILED
    )
    _set_status(monkeypatch, "code_review", EnumRangeVerdict.MET)
    handler, cid = _ready()
    _enable_one_climb(handler, monkeypatch)
    events = handler.handle_gate_result(_gate(cid, True))
    assert events[1].failure_class is EnumDelegationFailureClass.RUBRIC_FAILED
    assert "cited_lines_exist" in events[1].escalation_reason
    assert handler.workflows[cid].gate_result.fail_category == "rubric_failed"


def test_rubric_not_met_recorded_only_register_covers_criteria_and_lines():
    contract = load_delegation_class_rubrics()
    register = yaml.safe_load(DEFAULT_CHECK_REGISTER_PATH.read_text())
    assert validate_check_register(register) == []
    entries = {row["check_id"]: row for row in register["checks"]}
    for criteria in contract.classes.values():
        for criterion in criteria:
            assert (
                entries[f"delegation.rubric.criterion.{criterion.criterion_id}"][
                    "check_class"
                ]
                == "gate"
            )
    for task_class in contract.classes:
        line = contract.false_pass_lines[task_class]
        assert entries[line.check_id]["acceptance_line"] == line.model_dump(mode="json")


def test_rubric_not_met_recorded_only_config_unavailable(monkeypatch):
    def unavailable():
        raise ValueError("synthetic contract unavailable")

    monkeypatch.setattr(attempt_verdict, "_load_contract", unavailable)
    handler, cid = _ready()
    handler.handle_gate_result(_gate(cid, True))
    assert handler.workflows[cid].state is EnumDelegationState.COMPLETED


@pytest.mark.parametrize("status", [EnumRangeVerdict.REFUSED, EnumRangeVerdict.MET])
async def test_rubric_met_refuses_local_port_only_when_configured(
    tmp_path, monkeypatch, status
):
    from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
        ModelAttemptRubricVerdict,
    )
    from tests.unit.delegation.test_rubric_recorded_not_deciding_omn20165 import (
        _local,
        local_module,
    )

    contract = load_delegation_class_rubrics()
    raw = contract.model_dump(mode="json")
    # The synthetic class stands in for a newly measured class on the local
    # path, whose task contract and response floor already accept this answer.
    raw["classes"]["document"] = raw["classes"]["summarization"]
    raw["false_pass_status"]["document"] = status.value
    for field in ("false_pass_lines", "false_refusal_lines"):
        raw[field]["document"] = dict(raw[field]["summarization"])
    configured = type(contract).model_validate(raw)
    monkeypatch.setattr(attempt_verdict, "_load_contract", lambda: configured)
    monkeypatch.setattr(
        local_module,
        "record_attempt_rubric_verdict",
        lambda **_: ModelAttemptRubricVerdict(
            rubric_version=contract.rubric_version,
            task_class="document",
            outcome="FAIL",
            failed_criteria=("claims_traceable",),
        ),
    )
    result, effect = await _local(tmp_path, monkeypatch)
    attempt = result["attempts"][0]
    assert attempt["rubric_verdict"]["outcome"] == "FAIL"
    if status is EnumRangeVerdict.MET:
        assert result["status"] == "failed"
        assert attempt["failure_class"] == "rubric_failed"
        assert attempt["acceptance_reason"] == "rubric_failed"
        assert "claims_traceable" in attempt["acceptance_detail"]
        assert len(effect.calls) == 1  # advance the chain, no same-responder redraw
        assert result["response_contract_evidence"]["validated"] is False
    else:
        assert result["status"] == "completed"
        assert attempt["failure_class"] is None


def test_rubric_met_refuses_undetermined_criteria(monkeypatch):
    from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
        ModelAttemptRubricVerdict,
    )

    _set_status(monkeypatch, "code_review", EnumRangeVerdict.MET)
    handler, cid = _ready()
    _enable_one_climb(handler, monkeypatch)
    from omnimarket.nodes.node_delegation_orchestrator.handlers import (
        handler_delegation_workflow as workflow_module,
    )

    monkeypatch.setattr(
        workflow_module,
        "record_attempt_rubric_verdict",
        lambda **_: ModelAttemptRubricVerdict(
            rubric_version=load_delegation_class_rubrics().rubric_version,
            task_class="code_review",
            outcome="UNDETERMINED",
            undetermined_criteria=("rubric_check_error",),
        ),
    )
    handler.handle_gate_result(_gate(cid, True))
    attempt = handler.workflows[cid].escalation_history[0]
    assert attempt.failure_class == "rubric_failed"
    assert attempt.failure_reasons == ("rubric_failed: rubric_check_error",)


def test_rubric_failed_is_typed_failure_exhausted_chain(monkeypatch):
    from tests.unit.delegation.test_omn19436_attempts_record_finish_reason import (
        _no_further_rung,
    )

    _set_status(monkeypatch, "code_review", EnumRangeVerdict.MET)
    handler, cid = _ready()
    monkeypatch.setattr(
        handler,
        "_maybe_retry_local",
        lambda *_a, **_k: pytest.fail("rubric refusal must advance to next responder"),
    )
    monkeypatch.setattr(handler, "_decide_escalation", _no_further_rung)
    terminals = handler.handle_gate_result(_gate(cid, True))
    workflow = handler.workflows[cid]
    assert terminals
    assert workflow.state is EnumDelegationState.FAILED
    assert workflow.escalation_history[0].failure_class == "rubric_failed"
    assert "cited_lines_exist" in workflow.escalation_history[0].failure_reasons[0]
