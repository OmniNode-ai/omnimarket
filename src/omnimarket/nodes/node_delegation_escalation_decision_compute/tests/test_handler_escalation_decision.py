# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerEscalationDecision — pure deterministic escalation/tier decision.

OMN-13476: covers the contract's escalation cases — tier-ladder progression,
max-attempts -> escalate-no-more, refusal/non-retryable -> terminate,
quality-gate-fail -> escalate, success -> no-escalate (not this handler's path,
asserted via the orchestrator delegation), and ladder-exhaustion -> terminate.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.enums.enum_delegation_traffic_class import (
    EnumDelegationTrafficClass,
)
from omnibase_core.models.delegation.wire import (
    ModelDelegationFailed,
    ModelDelegationProvenance,
    ModelDelegationRequest,
    ModelInferenceResponseData,
    ModelRoutingIntent,
)
from pydantic import BaseModel, ValidationError

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.inference import provider_quota_state
from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.models.delegation.model_escalation_transitions import (
    EnumEscalationCause,
    EnumMoveKind,
    EnumTransitionKind,
)
from omnimarket.models.delegation.model_transition_decision import (
    ModelTransitionDecisionRequest,
)
from omnimarket.models.delegation.wire.model_probe_ladder import (
    PROBE_LADDER_SOURCE_SURFACE,
    ModelProbeLadder,
    ModelProbeRung,
)
from omnimarket.models.delegation.wire.model_quality_gate import (
    ModelQualityGateResult,
    ModelQualityRuleEvaluation,
)
from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.stub_probe_transport import (
    StubProbeReply,
    StubProbeTransport,
)
from omnimarket.nodes.node_delegation_escalation_decision_compute.handlers.handler_escalation_decision import (
    HandlerEscalationDecision,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hw,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    TaskClassTransitionsError,
    resolve_task_class_transitions,
)
from omnimarket.routing.model_escalation_decision_request import (
    ModelEscalationDecisionRequest,
)
from omnimarket.routing.model_escalation_decision_result import (
    ModelEscalationDecisionResult,
)
from omnimarket.routing.task_class_contract_path import (
    TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH,
)


def _req(**overrides: object) -> ModelEscalationDecisionRequest:
    base: dict[str, object] = {
        "escalation_count": 0,
        "max_escalation_attempts": 2,
        "current_tier_name": "local",
        "error_retryable": True,
        "next_tier_name": "cheap_cloud",
        "non_retryable_reason": "non_retryable_inference_response",
        "no_higher_tier_reason": None,
    }
    base.update(overrides)
    return ModelEscalationDecisionRequest.model_validate(base)


@pytest.mark.unit
class TestTierLadderProgression:
    def test_quality_gate_fail_escalates_to_next_tier(self) -> None:
        # quality-gate-fail path: error_retryable=True, a routable next tier.
        result = HandlerEscalationDecision().handle(
            _req(current_tier_name="local", next_tier_name="cheap_cloud")
        )
        assert result.can_escalate is True
        assert result.next_tier_name == "cheap_cloud"
        assert result.terminal_failure_reason is None

    def test_progression_advances_through_ladder(self) -> None:
        # second rung: local -> cheap_cloud already done, now cheap_cloud -> claude.
        result = HandlerEscalationDecision().handle(
            _req(
                escalation_count=1,
                current_tier_name="cheap_cloud",
                next_tier_name="claude",
            )
        )
        assert result.can_escalate is True
        assert result.next_tier_name == "claude"


@pytest.mark.unit
class TestMaxAttemptsTerminates:
    def test_at_max_attempts_does_not_escalate(self) -> None:
        result = HandlerEscalationDecision().handle(
            _req(escalation_count=2, max_escalation_attempts=2)
        )
        assert result.can_escalate is False
        assert result.terminal_failure_reason == "max_escalation_attempts_reached"
        assert result.next_tier_name is None

    def test_over_max_attempts_does_not_escalate(self) -> None:
        result = HandlerEscalationDecision().handle(
            _req(escalation_count=5, max_escalation_attempts=2)
        )
        assert result.can_escalate is False
        assert result.terminal_failure_reason == "max_escalation_attempts_reached"

    def test_budget_check_precedes_tier_resolution(self) -> None:
        # Even with a routable next tier, an exhausted budget terminates.
        result = HandlerEscalationDecision().handle(
            _req(
                escalation_count=2,
                max_escalation_attempts=2,
                next_tier_name="claude",
            )
        )
        assert result.can_escalate is False
        assert result.terminal_failure_reason == "max_escalation_attempts_reached"


@pytest.mark.unit
class TestNonRetryableTerminates:
    def test_non_retryable_error_terminates_with_supplied_reason(self) -> None:
        result = HandlerEscalationDecision().handle(
            _req(
                error_retryable=False,
                non_retryable_reason="non_retryable_inference_response",
                next_tier_name="claude",
            )
        )
        assert result.can_escalate is False
        assert result.terminal_failure_reason == "non_retryable_inference_response"
        assert result.next_tier_name is None

    def test_non_retryable_precedes_budget_and_tier(self) -> None:
        # Non-retryable wins even with budget and a routable tier available.
        result = HandlerEscalationDecision().handle(
            _req(
                error_retryable=False,
                escalation_count=0,
                max_escalation_attempts=99,
                next_tier_name="claude",
                non_retryable_reason="empty_choices_array",
            )
        )
        assert result.terminal_failure_reason == "empty_choices_array"


@pytest.mark.unit
class TestLadderExhaustionTerminates:
    def test_no_higher_tier_terminates_with_precise_reason(self) -> None:
        result = HandlerEscalationDecision().handle(
            _req(
                next_tier_name=None,
                no_higher_tier_reason="no_higher_tier_available: claude excluded",
            )
        )
        assert result.can_escalate is False
        assert (
            result.terminal_failure_reason
            == "no_higher_tier_available: claude excluded"
        )

    def test_no_higher_tier_without_reason_raises(self) -> None:
        with pytest.raises(ValueError, match="no_higher_tier_reason"):
            HandlerEscalationDecision().handle(
                _req(next_tier_name=None, no_higher_tier_reason=None)
            )


@pytest.mark.unit
class TestCurrentTierUnknownTerminates:
    def test_current_tier_none_terminates(self) -> None:
        result = HandlerEscalationDecision().handle(
            _req(current_tier_name=None, next_tier_name="claude")
        )
        assert result.can_escalate is False
        assert result.terminal_failure_reason == "current_tier_unknown"


@pytest.mark.unit
class TestDeterminismAndPurity:
    def test_same_input_same_output(self) -> None:
        handler = HandlerEscalationDecision()
        req = _req()
        first = handler.handle(req)
        second = handler.handle(req)
        assert first == second

    def test_result_is_frozen(self) -> None:
        result = HandlerEscalationDecision().handle(_req())
        with pytest.raises(ValidationError):
            result.can_escalate = False  # type: ignore[misc]


@pytest.mark.unit
class TestResultModelInvariant:
    def test_escalate_requires_next_tier(self) -> None:
        with pytest.raises(ValidationError):
            ModelEscalationDecisionResult(
                can_escalate=True,
                next_tier_name=None,
                terminal_failure_reason=None,
            )

    def test_terminate_requires_reason(self) -> None:
        with pytest.raises(ValidationError):
            ModelEscalationDecisionResult(
                can_escalate=False,
                next_tier_name=None,
                terminal_failure_reason=None,
            )

    def test_escalate_rejects_terminal_reason(self) -> None:
        with pytest.raises(ValidationError):
            ModelEscalationDecisionResult(
                can_escalate=True,
                next_tier_name="claude",
                terminal_failure_reason="oops",
            )


# ---------------------------------------------------------------------------
# OMN-21213: the declared transition table, decided here on both paths.
# ---------------------------------------------------------------------------

_CONTRACT = Path(TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH)
_LISTS = {
    "retry_on": EnumTransitionKind.RETRY,
    "move_on": EnumTransitionKind.MOVE,
    "skip_on": EnumTransitionKind.SKIP,
    "terminate_on": EnumTransitionKind.TERMINATE,
}
# A stub rung for each cause the node raises itself: what the rung did, and
# the run state that turns it into that cause.
_RAISED_BY_THE_NODE: dict[EnumEscalationCause, dict[str, object]] = {
    EnumEscalationCause.RETRY_BUDGET_SPENT: {
        "cause": EnumEscalationCause.TRANSIENT_PROVIDER_ERROR,
        "retries_on_rung": 1,
        "rung_retry_budget": 1,
    },
    EnumEscalationCause.PINNED_BACKEND_FAILED: {
        "cause": EnumEscalationCause.RATE_LIMITED,
        "pinned": True,
    },
    EnumEscalationCause.ESCALATION_LIMIT: {
        "cause": EnumEscalationCause.RATE_LIMITED,
        "escalation_count": 2,
        "max_escalations": 2,
    },
    EnumEscalationCause.LADDER_EXHAUSTED: {
        "cause": EnumEscalationCause.RATE_LIMITED,
        "next_rung_available": False,
    },
}


def _declared() -> list[tuple[str, str, EnumEscalationCause]]:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return [
        (task_class, list_name, EnumEscalationCause(cause))
        for task_class, entry in raw["task_classes"].items()
        for list_name in _LISTS
        for cause in entry["transitions"][list_name]
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_class", "list_name", "cause"),
    _declared(),
    ids=lambda value: getattr(value, "value", value),
)
def test_transition_table_each_declared_cause_yields_its_transition(
    task_class: str, list_name: str, cause: EnumEscalationCause
) -> None:
    transitions = resolve_task_class_transitions(task_class)
    produced = _RAISED_BY_THE_NODE.get(
        cause,
        {
            "cause": cause,
            "rung_retry_budget": 1 if list_name == "retry_on" else 0,
        },
    )

    decision = HandlerEscalationDecision().decide_transition(
        ModelTransitionDecisionRequest.model_validate(
            {"transitions": transitions, **produced}
        )
    )

    assert decision.kind is _LISTS[list_name]
    assert decision.cause is cause
    if decision.kind is EnumTransitionKind.MOVE:
        assert decision.move_kind is EnumMoveKind.TIER_CLIMB
    else:
        assert decision.move_kind is None


def _bind_contract(monkeypatch: pytest.MonkeyPatch, copy: Path) -> None:
    """Have the routing authority read ``copy`` instead of its cached contract."""
    monkeypatch.setattr(
        routing,
        "_get_task_class_contract",
        lambda: yaml.safe_load(copy.read_text(encoding="utf-8")),
    )


@pytest.mark.unit
@pytest.mark.parametrize("list_name", list(_LISTS))
def test_missing_lists_refuse_to_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, list_name: str
) -> None:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entry = raw["task_classes"]["research"]
    entry["transitions"] = {
        name: causes
        for name, causes in entry["transitions"].items()
        if name != list_name
    }
    copy = tmp_path / "task_class_contracts.v1.yaml"
    copy.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match=list_name):
        load_task_class_authority(copy)
    _bind_contract(monkeypatch, copy)
    with pytest.raises(TaskClassTransitionsError, match=list_name):
        resolve_task_class_transitions("research")


@pytest.mark.unit
def test_missing_lists_refuse_when_the_whole_block_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    del raw["task_classes"]["research"]["transitions"]
    copy = tmp_path / "task_class_contracts.v1.yaml"
    copy.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="transitions"):
        load_task_class_authority(copy)
    _bind_contract(monkeypatch, copy)
    with pytest.raises(TaskClassTransitionsError, match="refuses to load"):
        resolve_task_class_transitions("research")


# A stub rung on each path. ``bus`` drives the orchestrator's workflow handler
# with a failed provider call or a gate verdict; ``local`` drives the in-process
# port over a two-rung probe ladder answered by the stub transport.
_FIRST = "http://127.0.0.1:41011/v1/chat/completions"
_SECOND = "http://127.0.0.1:41012/v1/chat/completions"
_GOOD_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile this "
    "approach carries in practice."
)
_DOCTOR = ModelDelegationProvenance(
    source="claude-code",
    traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
    source_surface=PROBE_LADDER_SOURCE_SURFACE,
)
# What a provider call that failed with each cause looks like on each path.
_FAILED_CALL: dict[EnumEscalationCause, tuple[str, StubProbeReply]] = {
    EnumEscalationCause.RATE_LIMITED: (
        "provider http 429: rate limit exceeded",
        StubProbeReply.rate_limited(),
    ),
    EnumEscalationCause.RUNG_TIMEOUT: (
        "request timed out",
        StubProbeReply(failure_class=EnumDelegationFailureClass.TIMEOUT),
    ),
    EnumEscalationCause.RETRY_BUDGET_SPENT: (
        "provider http 503: upstream unavailable",
        StubProbeReply(
            failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
            http_status=503,
        ),
    ),
    EnumEscalationCause.NON_RETRYABLE_FAILURE: (
        "provider_billing: account cannot fund the call",
        StubProbeReply(failure_class=EnumDelegationFailureClass.INVALID_JSON),
    ),
}


@dataclass
class _PathRun:
    """What one path did after its first rung: the transition and the calls."""

    kind: EnumTransitionKind
    cause: str
    calls: int
    terminal_reason: str | None = None


def _ladder(task_type: str) -> ModelProbeLadder:
    del task_type
    return ModelProbeLadder(
        rungs=(
            ModelProbeRung(
                backend_id="probe-first", model_id="stub-a", endpoint_ref=_FIRST
            ),
            ModelProbeRung(
                backend_id="probe-second", model_id="stub-b", endpoint_ref=_SECOND
            ),
        )
    )


def _local_run(
    tmp_path: Path, task_type: str, first: StubProbeReply, second: StubProbeReply
) -> tuple[dict[str, Any], StubProbeTransport]:
    transport = StubProbeTransport(replies={_FIRST: first, _SECOND: second})
    port = LocalDelegationDispatchPort(
        effect_handler=transport,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
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
            provenance=_DOCTOR,
            probe_ladder=_ladder(task_type),
        )
    )
    return result, transport


def _local_first_rung(
    tmp_path: Path, task_type: str, first: StubProbeReply, second: StubProbeReply
) -> _PathRun:
    result, transport = _local_run(tmp_path, task_type, first, second)
    transition = result["attempts"][0]["transition"]
    return _PathRun(
        kind=EnumTransitionKind(transition["kind"]),
        cause=transition["cause"],
        calls=len(transport.calls),
        terminal_reason=None if result["status"] == "completed" else "failed",
    )


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> hw.HandlerDelegationWorkflow:
    # Node-local tests do not inherit tests/conftest.py's ambient-tenant
    # isolation. This fixture has no tenant registry and names no tenant.
    monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
    monkeypatch.setattr(
        provider_quota_state,
        "resolve_provider_quota_reader",
        lambda **_: provider_quota_state.StaticProviderQuotaReader(()),
    )
    monkeypatch.setattr(hw, "next_eligible_tier", lambda *_a, **_k: "cheap_cloud")
    # This fixture declares two tiers and no same-tier sibling. Do not let the
    # host's installed backend overlay add an unmodelled third responder.
    monkeypatch.setattr(hw, "sibling_backend_available_in_tier", lambda *_a, **_k: None)
    return hw.HandlerDelegationWorkflow(workflows={})


def _bus_route(
    handler: hw.HandlerDelegationWorkflow, task_type: str, tier: str = "local"
) -> UUID:
    cid = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt="explain the tradeoff",
            task_type=task_type,
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
        )
    )
    _bus_decision(handler, cid, task_type, tier)
    return cid


def _bus_decision(
    handler: hw.HandlerDelegationWorkflow, cid: UUID, task_type: str, tier: str
) -> None:
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
            system_prompt="Answer the question.",
            rationale="Controlled route for the transition table.",
            tier_name=tier,
            dod_deterministic=("compiles_without_errors", "response_non_empty"),
        )
    )


def _bus_outcome(
    handler: hw.HandlerDelegationWorkflow, cid: UUID, events: list[BaseModel]
) -> _PathRun:
    failed = next((e for e in events if isinstance(e, ModelDelegationFailed)), None)
    routed = next((e for e in events if isinstance(e, ModelRoutingIntent)), None)
    workflow = handler.workflows[cid]
    if failed is not None:
        return _PathRun(
            kind=EnumTransitionKind.TERMINATE,
            cause="",
            calls=1,
            terminal_reason=failed.terminal_failure_reason,
        )
    assert routed is not None
    kind = (
        EnumTransitionKind.MOVE
        if workflow.escalation_count == 1
        else EnumTransitionKind.RETRY
    )
    return _PathRun(kind=kind, cause="", calls=2)


def _bus_failed_call(
    handler: hw.HandlerDelegationWorkflow, task_type: str, error_message: str
) -> _PathRun:
    cid = _bus_route(handler, task_type)
    if error_message.startswith("provider_billing:"):
        # A customer's billing refusal is terminal; an empty completion can
        # now move and therefore cannot stand in for NON_RETRYABLE_FAILURE.
        workflow = handler.workflows[cid]
        assert workflow.routing_decision is not None
        workflow.routing_decision = workflow.routing_decision.model_copy(
            update={"selected_backend_ref": "byok-openrouter"}
        )
    events = handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="",
            model_used="fixture-model",
            latency_ms=10,
            error_message=error_message,
        )
    )
    return _bus_outcome(handler, cid, events)


def _bus_gate(
    handler: hw.HandlerDelegationWorkflow,
    task_type: str,
    *,
    fail_category: str,
    rule: str | None = None,
    cid: UUID | None = None,
) -> tuple[UUID, _PathRun]:
    cid = cid or _bus_route(handler, task_type)
    contract = handler.workflows[cid].effective_deliverable_contract
    assert contract is not None
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content=f"{contract.render_start_marker}\nThe change adds retry checks.",
            model_used="fixture-model",
            latency_ms=10,
        )
    )
    events = handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=cid,
            passed=False,
            quality_score=0.4,
            fail_category=fail_category,
            failure_reasons=("refused",),
            fallback_recommended=True,
            rule_evaluations=(
                (
                    ModelQualityRuleEvaluation(
                        rule=rule, enforcement="blocking", passed=False, detail="x"
                    ),
                )
                if rule is not None
                else ()
            ),
            finish_reason=EnumProviderFinishReason.STOP,
        )
    )
    return cid, _bus_outcome(handler, cid, events)


@pytest.mark.unit
@pytest.mark.parametrize("cause", list(_FAILED_CALL), ids=lambda c: c.value)
@pytest.mark.parametrize("path", ["bus", "local"])
def test_transition_table_a_failed_call_takes_its_declared_transition_on_each_path(
    path: str,
    cause: EnumEscalationCause,
    tmp_path: Path,
    bus: hw.HandlerDelegationWorkflow,
) -> None:
    error_message, reply = _FAILED_CALL[cause]
    expected = resolve_task_class_transitions("research").kind_of(cause)

    if path == "local":
        run = _local_first_rung(
            tmp_path, "research", reply, StubProbeReply.answer(_GOOD_ANSWER)
        )
        assert run.cause == cause.value
    else:
        run = _bus_failed_call(bus, "research", error_message)

    assert run.kind is expected
    if expected is EnumTransitionKind.MOVE:
        assert run.calls == 2
    else:
        # A terminal makes no later provider call.
        assert run.calls == 1
        assert run.terminal_reason is not None


@pytest.mark.unit
@pytest.mark.parametrize("path", ["bus", "local"])
def test_transition_table_a_refused_answer_redrafts_the_free_rung_on_each_path(
    path: str, tmp_path: Path, bus: hw.HandlerDelegationWorkflow
) -> None:
    transitions = resolve_task_class_transitions("research")
    assert (
        transitions.kind_of(EnumEscalationCause.QUALITY_GATE_REFUSED)
        is EnumTransitionKind.RETRY
    )
    if path == "local":
        result, transport = _local_run(
            tmp_path,
            "research",
            StubProbeReply.answer("### ANSWER\nno."),
            StubProbeReply.answer(_GOOD_ANSWER),
        )
        first = result["attempts"][0]["transition"]
        assert first == {
            "kind": "retry",
            "cause": "quality_gate_refused",
            "move_kind": None,
        }
        # The free rung re-drafts on itself before the run moves.
        assert transport.calls[:2] == [_FIRST, _FIRST]
    else:
        _, run = _bus_gate(bus, "research", fail_category="fail_heuristic")
        assert run.kind is EnumTransitionKind.RETRY


@pytest.mark.unit
@pytest.mark.parametrize("path", ["bus", "local"])
def test_transition_table_a_move_is_recorded_with_its_kind_and_cause(
    path: str, tmp_path: Path, bus: hw.HandlerDelegationWorkflow
) -> None:
    if path == "local":
        result, transport = _local_run(
            tmp_path,
            "research",
            StubProbeReply.rate_limited(),
            StubProbeReply.answer(_GOOD_ANSWER),
        )
        assert result["status"] == "completed"
        assert transport.calls == [_FIRST, _SECOND]
        assert result["attempts"][0]["transition"] == {
            "kind": "move",
            "cause": "rate_limited",
            "move_kind": "tier_climb",
        }
        # The evidence row is written, and its attempt names the move. The
        # released wire record has no typed field for it yet.
        connection = sqlite3.connect(tmp_path / "delegation.sqlite")
        try:
            (history,) = connection.execute(
                "SELECT attempt_history FROM delegation_events"
            ).fetchone()
        finally:
            connection.close()
        assert (
            "transition=move:rate_limited:tier_climb"
            in (json.loads(history)[0]["acceptance_detail"])
        )
    else:
        cid = _bus_route(bus, "research")
        events = bus.handle_inference_response(
            ModelInferenceResponseData(
                correlation_id=cid,
                content="",
                model_used="fixture-model",
                latency_ms=10,
                error_message="provider http 429: rate limit exceeded",
            )
        )
        routed = next(e for e in events if isinstance(e, ModelRoutingIntent))
        assert routed.min_tier_name == "cheap_cloud"
        assert bus.workflows[cid].escalation_count == 1


@pytest.mark.unit
@pytest.mark.parametrize("path", ["bus", "local"])
def test_transition_table_an_exhausted_ladder_terminates_with_no_later_call(
    path: str,
    tmp_path: Path,
    bus: hw.HandlerDelegationWorkflow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if path == "local":
        result, transport = _local_run(
            tmp_path,
            "research",
            StubProbeReply.rate_limited(),
            StubProbeReply.rate_limited(),
        )
        assert result["status"] == "failed"
        assert transport.calls == [_FIRST, _SECOND]
        assert result["attempts"][-1]["transition"]["kind"] == "terminate"
        assert result["attempts"][-1]["transition"]["cause"] in {
            "ladder_exhausted",
            "escalation_limit",
        }
    else:
        monkeypatch.setattr(hw, "next_eligible_tier", lambda *_a, **_k: None)
        run = _bus_failed_call(bus, "research", "provider http 429: rate limit")
        assert run.kind is EnumTransitionKind.TERMINATE
        assert run.terminal_reason is not None
        assert run.terminal_reason.startswith("no_higher_tier_available")


@pytest.mark.unit
@pytest.mark.parametrize("path", ["bus", "local"])
def test_floor_veto_a_repeated_deterministic_floor_stops_with_no_further_call(
    path: str, tmp_path: Path, bus: hw.HandlerDelegationWorkflow
) -> None:
    transitions = resolve_task_class_transitions("documentation")
    assert (
        transitions.kind_of(EnumEscalationCause.QUALITY_VETO)
        is EnumTransitionKind.TERMINATE
    )
    if path == "local":
        prose = StubProbeReply.answer("### ANSWER\nThis function adds two numbers.")
        result, transport = _local_run(tmp_path, "documentation", prose, prose)
        assert result["status"] == "failed"
        # The first rung's re-draft fails the same floor, so the ladder stops
        # there and the second rung is never called.
        assert transport.calls == [_FIRST, _FIRST]
        last = result["attempts"][-1]
        assert last["acceptance_reason"] == "deterministic_floor_failed"
        assert last["transition"] == {
            "kind": "terminate",
            "cause": "quality_veto",
            "move_kind": None,
        }
    else:
        cid, first = _bus_gate(
            bus,
            "code_generation",
            fail_category="fail_deterministic",
            rule="compiles_without_errors",
        )
        assert first.kind is not EnumTransitionKind.TERMINATE
        workflow = bus.workflows[cid]
        _bus_decision(
            bus, cid, "code_generation", workflow.current_tier_name or "local"
        )
        _, second = _bus_gate(
            bus,
            "code_generation",
            fail_category="fail_deterministic",
            rule="compiles_without_errors",
            cid=cid,
        )
        assert second.kind is EnumTransitionKind.TERMINATE
        assert second.terminal_reason == "repeated_deterministic_floor"
