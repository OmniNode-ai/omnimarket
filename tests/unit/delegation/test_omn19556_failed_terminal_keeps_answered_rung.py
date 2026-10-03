# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19556: a failed delegate-skill terminal keeps the best answered rung.

Run 45661dd1 answered on three local rungs, the quality gate refused each at
0.567, the ladder climbed to glm-5.3-flash, and that call timed out. The failed
terminal carried the timed-out call's empty text as its response, so two correct
local answers were dropped and the caller could not tell which rung a response
came from.

The shape is rebuilt through the real orchestrator FSM (only routing policy is
controlled; responses, gates, history and terminals are real), and the failed
terminal it emits is then replayed through the real ``HandlerDelegateSkill`` so
the assertions read the delegate-skill failed terminal a caller receives.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.delegation.wire import EnumDelegationOperationalOutcome
from pydantic import BaseModel

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
    ProtocolDelegationDispatchPort,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hw,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationFailed,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.state_codec import decode, encode
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.routing.model_escalation_decision_result import (
    ModelEscalationDecisionResult,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

_TIMEOUT = "The read operation timed out while calling provider [ReadTimeout]"
_DRAFTS = (
    "def test_first_local():\n    assert 1 + 1 == 2",
    "def test_second_local():\n    assert 2 + 2 == 4",
    "def test_third_local():\n    assert 3 + 3 == 6",
    "def test_first_frontier():\n    assert 4 + 4 == 8",
    "def test_second_frontier():\n    assert 5 + 5 == 10",
)


class _ReplayPort:
    """Dispatch port that replays one orchestrator terminal payload verbatim."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    async def dispatch(self, **_kwargs: object) -> dict[str, object]:
        return dict(self._payload)


def _delegate_skill_terminal(
    failed: ModelDelegationFailed,
) -> ModelDelegateSkillFailed:
    """The delegate-skill terminal a caller receives for this failed ladder."""
    port = cast(
        ProtocolDelegationDispatchPort,
        _ReplayPort(failed.model_dump(mode="json")),
    )
    handler = HandlerDelegateSkill(dispatch_port=port)
    terminal = asyncio.run(
        handler.handle(
            ModelDelegateSkillRequest(
                prompt="Write unit tests for verify_registration.py",
                task_type="test",
                source="external-client",
                correlation_id=failed.correlation_id,
            )
        )
    )
    assert isinstance(terminal, ModelDelegateSkillFailed)
    return terminal


def _source_attempts(payload: dict[str, object]) -> list[tuple[int, str, str]]:
    """(index, tier, backend) of every history entry marked as the response source."""
    history = payload["escalation_history"]
    assert isinstance(history, list)
    return [
        (index, str(attempt["tier_name"]), str(attempt["backend_ref"]))
        for index, attempt in enumerate(history)
        if attempt.get("supplied_response") is True
    ]


def _route(cid: UUID, tier: str, backend: str) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type="test",
        selected_model=backend,
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{backend}"),
        selected_backend_ref=backend,
        endpoint_url="http://provider.invalid:8000",
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale=f"Fixture route to {tier}/{backend}",
        tier_name=tier,
    )


@pytest.fixture
def ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[hw.HandlerDelegationWorkflow, UUID]:
    """Use real same-tier retry and sibling mechanics with deterministic policy."""
    handler = hw.HandlerDelegationWorkflow(workflows={})
    monkeypatch.setattr(
        hw, "is_free_tier", lambda tier: tier in {"local", "cheap_frontier"}
    )
    monkeypatch.setattr(
        hw,
        "tier_max_retries",
        lambda tier: {"local": 2, "cheap_frontier": 1}.get(tier, 0),
    )
    monkeypatch.setattr(
        hw,
        "sibling_backend_available_in_tier",
        lambda tier, _task, excluded: (
            "glm-5.3-flash"
            if tier == "cheap_cloud" and "glm-5.3-flash" not in excluded
            else None
        ),
    )

    def decide(
        workflow: hw.DelegationWorkflowState, **_kwargs: object
    ) -> ModelEscalationDecisionResult:
        next_tier = {"local": "cheap_frontier", "cheap_frontier": "cheap_cloud"}.get(
            workflow.current_tier_name or ""
        )
        if next_tier is not None:
            return ModelEscalationDecisionResult(
                can_escalate=True, next_tier_name=next_tier
            )
        return ModelEscalationDecisionResult(
            can_escalate=False, terminal_failure_reason="fixture_ladder_exhausted"
        )

    monkeypatch.setattr(handler, "_decide_escalation", decide)
    cid = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt="Write unit tests for verify_registration.py",
            task_type="test",
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
        )
    )
    return handler, cid


def _assert_reroute(events: list[BaseModel], tier: str) -> None:
    routes = [event for event in events if isinstance(event, ModelRoutingIntent)]
    assert len(routes) == 1
    assert routes[0].min_tier_name == tier
    assert not any(isinstance(event, ModelDelegationFailed) for event in events)


def _answer(
    handler: hw.HandlerDelegationWorkflow,
    cid: UUID,
    index: int,
    content: str,
    score: float,
    *,
    truncated: bool = False,
) -> list[BaseModel]:
    tier = "local" if index < 3 else "cheap_frontier"
    handler.handle_routing_decision(_route(cid, tier, f"{tier}-backend"))
    contract = handler.workflows[cid].effective_deliverable_contract
    assert contract is not None
    assert contract.render_start_marker is not None
    # Give extraction the contract's boundary, so these are real retained
    # drafts rather than unmarked responses blanked before the gate runs.
    raw_content = f"{contract.render_start_marker}\n{content}" if content else ""
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content=raw_content,
            model_used=f"{tier}-backend",
            latency_ms=10,
        )
    )
    assert handler.workflows[cid].state is EnumDelegationState.INFERENCE_COMPLETED
    assert handler.workflows[cid].inference_content == content
    return handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=cid,
            passed=False,
            quality_score=score,
            fail_category="fail_deterministic",
            failure_reasons=("deterministic_floor_failed",),
            fallback_recommended=True,
            finish_reason=EnumProviderFinishReason.LENGTH
            if truncated
            else EnumProviderFinishReason.STOP,
        )
    )


def _provider_failure(
    handler: hw.HandlerDelegationWorkflow, cid: UUID, tier: str, backend: str
) -> list[BaseModel]:
    handler.handle_routing_decision(_route(cid, tier, backend))
    return handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="",
            model_used=backend,
            latency_ms=10,
            error_message=_TIMEOUT,
        )
    )


def test_run_45661dd1_three_refused_local_answers_then_a_provider_timeout(
    ladder: tuple[hw.HandlerDelegationWorkflow, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: the recorded attempts, with the terminal read as a caller reads it.

    Three local answers refused at 0.567, then the glm-5.3-flash call timed out.
    Before the change the terminal response was the timed-out call's empty text
    and quality_score was null.
    """
    handler, cid = ladder
    # The recorded ladder climbed from the third refused local rung straight to
    # the cloud rung that timed out.
    monkeypatch.setattr(
        handler,
        "_decide_escalation",
        lambda *_a, **_kw: ModelEscalationDecisionResult(
            can_escalate=True, next_tier_name="cheap_cloud"
        ),
    )
    for index in range(3):
        _assert_reroute(
            _answer(handler, cid, index, _DRAFTS[index], 0.567),
            "local" if index < 2 else "cheap_cloud",
        )
    # glm-5.3-flash is the last rung: no sibling backend and no higher tier.
    monkeypatch.setattr(hw, "sibling_backend_available_in_tier", lambda *_a: None)
    monkeypatch.setattr(
        handler,
        "_decide_escalation",
        lambda *_a, **_kw: ModelEscalationDecisionResult(
            can_escalate=False, terminal_failure_reason="fixture_ladder_exhausted"
        ),
    )
    events = _provider_failure(handler, cid, "cheap_cloud", "glm-5.3-flash")
    failed = [event for event in events if isinstance(event, ModelDelegationFailed)]
    assert len(failed) == 1
    payload = failed[0].model_dump(mode="json")
    history = payload["escalation_history"]
    assert [attempt["acceptance_reason"] for attempt in history] == [
        "deterministic_floor_failed"
    ] * 3 + ["provider_call_failed"]
    assert payload["content"] == _DRAFTS[0]
    assert _source_attempts(payload) == [(0, "local", history[0]["backend_ref"])]
    terminal = _delegate_skill_terminal(failed[0])
    assert terminal.response == _DRAFTS[0]
    assert terminal.status == "failed"


@pytest.mark.parametrize(
    ("scores", "truncated_index", "empty_index", "source_index"),
    [
        pytest.param(
            (0.567, 0.567, 0.567, 0.433, 0.433),
            None,
            None,
            0,
            id="recorded_shape_first_wins_ties",
        ),
        pytest.param(
            (0.3, 0.567, 0.567, 0.433, 0.433),
            None,
            None,
            1,
            id="highest_score_then_first_tie",
        ),
        pytest.param(
            (0.3, 0.4, 0.5, 0.6, 0.567), None, None, 3, id="best_answer_can_be_frontier"
        ),
        pytest.param(
            (0.9, 0.567, 0.5, 0.433, 0.433),
            0,
            None,
            1,
            id="exclude_output_length_truncation",
        ),
        pytest.param(
            (0.9, 0.567, 0.5, 0.433, 0.433), None, 0, 1, id="exclude_empty_draft"
        ),
    ],
)
def test_failed_terminal_keeps_best_answered_rung(
    ladder: tuple[hw.HandlerDelegationWorkflow, UUID],
    scores: tuple[float, ...],
    truncated_index: int | None,
    empty_index: int | None,
    source_index: int,
) -> None:
    handler, cid = ladder
    for index, score in enumerate(scores):
        events = _answer(
            handler,
            cid,
            index,
            "" if index == empty_index else _DRAFTS[index],
            score,
            truncated=index == truncated_index,
        )
        _assert_reroute(
            events,
            ("local", "local", "cheap_frontier", "cheap_frontier", "cheap_cloud")[
                index
            ],
        )
    _assert_reroute(
        _provider_failure(handler, cid, "cheap_cloud", "gemini-2.5-flash"),
        "cheap_cloud",
    )
    events = _provider_failure(handler, cid, "cheap_cloud", "glm-5.3-flash")
    failed = [event for event in events if isinstance(event, ModelDelegationFailed)]
    assert len(failed) == 1
    payload = failed[0].model_dump(mode="json")
    history = payload["escalation_history"]
    assert len(history) == payload["attempts_count"] == 7
    assert [attempt["tier_name"] for attempt in history] == ["local"] * 3 + [
        "cheap_frontier"
    ] * 2 + ["cheap_cloud"] * 2
    assert [attempt["quality_score"] for attempt in history] == [*scores, 0.0, 0.0]
    assert [attempt["acceptance_reason"] for attempt in history] == [
        "deterministic_floor_failed"
    ] * 5 + ["provider_call_failed"] * 2
    if truncated_index is not None:
        assert history[truncated_index]["truncated"] is True
    assert failed[0].operational_outcome is EnumDelegationOperationalOutcome.TIMEOUT
    assert failed[0].failure_reason == _TIMEOUT
    assert payload["content"] == _DRAFTS[source_index], (
        "the last provider failure must not erase the best answered draft"
    )
    assert _source_attempts(payload) == [
        (
            source_index,
            history[source_index]["tier_name"],
            history[source_index]["backend_ref"],
        )
    ]
    terminal = _delegate_skill_terminal(failed[0])
    assert terminal.response == _DRAFTS[source_index]
    assert terminal.status == "failed"
    assert terminal.quality_gate_passed is False


def test_gate_failed_terminal_with_content_names_its_source(
    ladder: tuple[hw.HandlerDelegationWorkflow, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final gate-refused answer also needs provenance, without a fallback."""
    handler, cid = ladder
    monkeypatch.setattr(handler, "_maybe_retry_local", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        handler,
        "_decide_escalation",
        lambda *_a, **_kw: ModelEscalationDecisionResult(
            can_escalate=False, terminal_failure_reason="fixture_ladder_exhausted"
        ),
    )
    events = _answer(handler, cid, 0, _DRAFTS[0], 0.567)
    failed = [event for event in events if isinstance(event, ModelDelegationFailed)]
    assert len(failed) == 1
    payload = failed[0].model_dump(mode="json")
    assert payload["content"] == _DRAFTS[0]
    assert _source_attempts(payload) == [
        (0, "local", payload["escalation_history"][0]["backend_ref"])
    ]
    assert _delegate_skill_terminal(failed[0]).response == _DRAFTS[0]


def test_all_provider_failures_keep_empty_content_and_no_source(
    ladder: tuple[hw.HandlerDelegationWorkflow, UUID],
) -> None:
    """AC3: preserving drafts must not manufacture an answer or success."""
    handler, cid = ladder
    for tier, backend, next_tier in (
        ("local", "local-backend", "cheap_frontier"),
        ("cheap_frontier", "cheap_frontier-backend", "cheap_cloud"),
        ("cheap_cloud", "gemini-2.5-flash", "cheap_cloud"),
    ):
        _assert_reroute(_provider_failure(handler, cid, tier, backend), next_tier)
    events = _provider_failure(handler, cid, "cheap_cloud", "glm-5.3-flash")
    failed = [event for event in events if isinstance(event, ModelDelegationFailed)]
    assert len(failed) == 1
    payload = failed[0].model_dump(mode="json")
    assert len(payload["escalation_history"]) == payload["attempts_count"] == 4
    assert all(
        attempt["acceptance_reason"] == "provider_call_failed"
        for attempt in payload["escalation_history"]
    )
    assert payload["content"] == ""
    assert _source_attempts(payload) == []
    assert failed[0].operational_outcome is EnumDelegationOperationalOutcome.TIMEOUT
    assert handler.workflows[cid].state is EnumDelegationState.FAILED
    terminal = _delegate_skill_terminal(failed[0])
    assert terminal.response == ""
    assert terminal.status == "failed"
    assert terminal.quality_gate_passed is False


def test_banked_draft_survives_the_durable_state_roundtrip(
    ladder: tuple[hw.HandlerDelegationWorkflow, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workflow is stored between events, so the banked answer must be too."""
    handler, cid = ladder
    _assert_reroute(_answer(handler, cid, 0, _DRAFTS[0], 0.567), "local")
    banked = handler.workflows[cid].best_answered_draft
    assert banked is not None
    restored = decode(encode(handler.workflows[cid]))
    assert restored.best_answered_draft == banked
    handler.workflows[cid] = restored
    monkeypatch.setattr(hw, "sibling_backend_available_in_tier", lambda *_a: None)
    monkeypatch.setattr(
        handler,
        "_decide_escalation",
        lambda *_a, **_kw: ModelEscalationDecisionResult(
            can_escalate=False, terminal_failure_reason="fixture_ladder_exhausted"
        ),
    )
    events = _provider_failure(handler, cid, "local", "local-backend")
    failed = [event for event in events if isinstance(event, ModelDelegationFailed)]
    assert len(failed) == 1
    payload = failed[0].model_dump(mode="json")
    assert payload["content"] == _DRAFTS[0]
    assert _source_attempts(payload) == [
        (0, "local", payload["escalation_history"][0]["backend_ref"])
    ]
    assert "response_source_attempt" not in payload
