# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19004: the rung that DECIDED a run sets its cause, not the last error.

Replayed from the terminals the deployed dev lane actually emitted, not from
hand-built inputs. Each fixture under ``tests/fixtures/receipts/omn19004_*``
is the ``delegate-skill-failed`` payload exactly as its receipt recorded it,
wrong cause included:

* ``73aba966``: five rungs, all answered, all refused by the gate on a
  deterministic floor. Emitted ``provider_error``.
* ``6ce51f77``: three rungs answered and refused by the gate, the fourth hit a
  genuine HTTP 429. Emitted ``provider_quota_exhausted``.
* ``ebfce7f3``: the one-word READY probe, answered on four rungs, refused by
  the ``semantic_adequacy`` fragment rule each time. Emitted ``provider_error``.

The vocabulary to say what happened exists now (omnibase_core 0.47.22 carries
``quality_gate_refused``), so the earlier allowance that kept these terminals
constructible is gone: a provider cause over a gate-decided record is refused
at construction, with the contradiction named.

The positive controls matter as much as the fixtures. A ladder the provider
decided keeps its provider cause, and the 429 on ``6ce51f77`` stays legible on
its own rung.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)
from pydantic import ValidationError

from omnimarket.delegation.deciding_cause import (
    is_gate_refusal,
    ladder_is_gate_decided,
)
from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
    resolve_terminal_failure_cause,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_escalation_attempt import (
    ModelDelegationEscalationAttempt,
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
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.routing.model_escalation_decision_result import (
    ModelEscalationDecisionResult,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "receipts"

_REPLAYS = [
    pytest.param(
        "omn19004_gate_refused_every_rung_73aba966.json",
        "provider_error",
        id="73aba966_five_rungs_all_refused_by_the_gate",
    ),
    pytest.param(
        "omn19004_gate_decided_then_429_6ce51f77.json",
        "provider_quota_exhausted",
        id="6ce51f77_gate_refused_three_then_a_real_429",
    ),
    pytest.param(
        "omn19004_ready_probe_fragment_ebfce7f3.json",
        "provider_error",
        id="ebfce7f3_ready_probe_refused_as_a_fragment",
    ),
]

_GATE = EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED


def _load(name: str) -> dict[str, Any]:
    path = _FIXTURES / name
    if not path.exists():  # pragma: no cover - a missing replay is a failure
        raise AssertionError(f"replay fixture missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))["terminal_payload"]
    assert isinstance(payload, dict)
    return payload


def _attempts(payload: dict[str, Any]) -> list[ModelDelegateSkillAttemptRecord]:
    return [
        ModelDelegateSkillAttemptRecord.model_validate(raw)
        for raw in payload["attempts"]
    ]


# ---------------------------------------------------------------------------
# The replays. Each was emitted with a provider cause; each was gate-decided.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fixture", "emitted_cause"), _REPLAYS)
def test_the_recorded_ladder_resolves_to_the_gate(
    fixture: str, emitted_cause: str
) -> None:
    """RED: the resolver read the gate's refusal text as a provider fault."""
    payload = _load(fixture)
    assert payload["terminal_failure_cause"] == emitted_cause, (
        "the fixture must carry the cause the lane actually emitted"
    )

    cause = resolve_terminal_failure_cause(
        _attempts(payload), error_message=str(payload.get("error_message") or "")
    )

    assert cause is _GATE


@pytest.mark.parametrize(("fixture", "emitted_cause"), _REPLAYS)
def test_the_emitted_terminal_is_refused_with_the_contradiction_named(
    fixture: str, emitted_cause: str
) -> None:
    """RED: the terminal as emitted constructed, provider cause and all.

    This is the clause that makes the rule an invariant rather than a lint. A
    producer that does not call the resolver still cannot publish the shape.
    """
    payload = _load(fixture)

    with pytest.raises(ValidationError) as excinfo:
        ModelDelegateSkillResponse.model_validate(payload)

    message = str(excinfo.value)
    assert emitted_cause in message, message
    assert "quality_gate_refused" in message, message


@pytest.mark.parametrize(("fixture", "emitted_cause"), _REPLAYS)
def test_the_same_terminal_with_the_deciding_cause_constructs(
    fixture: str, emitted_cause: str
) -> None:
    """The truthful value exists and is assignable; nothing is left as None."""
    del emitted_cause
    payload = {**_load(fixture), "terminal_failure_cause": _GATE.value}

    model = ModelDelegateSkillResponse.model_validate(payload)

    assert model.terminal_failure_cause is _GATE


def test_the_real_429_stays_on_its_own_rung() -> None:
    """AC3: the provider fault that did not decide the run is still legible."""
    payload = _load("omn19004_gate_decided_then_429_6ce51f77.json")
    attempts = _attempts(payload)

    last = attempts[-1]
    assert last.acceptance_reason is EnumDelegationAcceptanceReason.PROVIDER_CALL_FAILED
    assert "429" in last.error_message
    gate_refused = [
        attempt
        for attempt in attempts
        if is_gate_refusal(attempt.acceptance_decision, attempt.acceptance_reason)
    ]
    assert len(gate_refused) == 3


class _FrozenDispatchPort:
    """Dispatch port replaying one recorded terminal verbatim."""

    def __init__(self, result: dict[str, object]) -> None:
        self._result = result

    async def dispatch(self, **_kwargs: object) -> dict[str, object]:
        return dict(self._result)


@pytest.mark.parametrize(("fixture", "emitted_cause"), _REPLAYS)
def test_the_handler_replay_names_the_gate(fixture: str, emitted_cause: str) -> None:
    """RED: the real handler, fed the recorded terminal, published a provider cause.

    The dispatch port hands back an EXPLICIT cause, and that explicit value
    used to win over anything derived. The record decides now.
    """
    del emitted_cause
    payload = _load(fixture)
    handler = HandlerDelegateSkill(dispatch_port=_FrozenDispatchPort(payload))

    terminal = asyncio.run(
        handler.handle(
            ModelDelegateSkillRequest(
                prompt=str(payload["prompt_text"]),
                task_type=str(payload["task_type"]),
                source="external-client",
                correlation_id=UUID(str(payload["correlation_id"])),
            )
        )
    )

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.terminal_failure_cause is _GATE
    assert terminal.quality_gate_passed is False


# ---------------------------------------------------------------------------
# Positive controls: a ladder the provider decided keeps its provider cause.
# ---------------------------------------------------------------------------


def _rung(
    *,
    decision: EnumDelegationAcceptanceDecision | None,
    reason: EnumDelegationAcceptanceReason | None,
    error_message: str = "",
    passed: bool = False,
) -> ModelDelegateSkillAttemptRecord:
    return ModelDelegateSkillAttemptRecord(
        tier="cheap_cloud",
        backend_id="backend-under-test",
        model_id="model-under-test",
        quality_gate_passed=passed,
        acceptance_decision=decision,
        acceptance_reason=reason,
        error_message=error_message,
    )


_CALL_FAILED = EnumDelegationAcceptanceReason.PROVIDER_CALL_FAILED
_CLIMB = EnumDelegationAcceptanceDecision.CLIMB


def test_a_ladder_of_real_429s_still_reads_quota_exhausted() -> None:
    """AC4: the provider-only case is not swept into the gate member."""
    attempts = [
        _rung(
            decision=_CLIMB,
            reason=_CALL_FAILED,
            error_message="provider HTTP 429 RESOURCE_EXHAUSTED: quota exceeded",
        )
        for _ in range(3)
    ]

    cause = resolve_terminal_failure_cause(attempts)

    assert cause is EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED


def test_a_rejected_credential_still_reads_auth_failed() -> None:
    attempts = [
        _rung(
            decision=_CLIMB,
            reason=_CALL_FAILED,
            error_message="HTTP 401 Authentication Failed",
        )
    ]

    assert (
        resolve_terminal_failure_cause(attempts)
        is EnumDelegationTerminalFailureCause.AUTH_FAILED
    )


def test_an_unresolved_bar_is_not_a_gate_verdict() -> None:
    """No bar resolved means no judgement happened; that is not the gate refusing."""
    attempts = [
        _rung(
            decision=_CLIMB,
            reason=EnumDelegationAcceptanceReason.REQUIRED_BAR_UNRESOLVED,
            error_message="required_bar_missing",
        )
    ]

    assert (
        resolve_terminal_failure_cause(attempts)
        is EnumDelegationTerminalFailureCause.PROVIDER_ERROR
    )


def test_a_rung_with_no_recorded_decision_is_not_read_as_a_refusal() -> None:
    """A record predating the typed pair keeps the old reading, not a guess."""
    attempts = [_rung(decision=None, reason=None, error_message="connection reset")]

    assert (
        resolve_terminal_failure_cause(attempts)
        is EnumDelegationTerminalFailureCause.PROVIDER_ERROR
    )


def test_an_accepted_ladder_has_no_cause_even_after_a_gate_refusal() -> None:
    """A gate refusal the ladder climbed past did not decide a run that succeeded."""
    attempts = [
        _rung(
            decision=_CLIMB,
            reason=EnumDelegationAcceptanceReason.SCORE_BELOW_REQUIRED_BAR,
            error_message="score below bar",
        ),
        _rung(
            decision=EnumDelegationAcceptanceDecision.ACCEPT,
            reason=EnumDelegationAcceptanceReason.QUALITY_BAR_MET,
            passed=True,
        ),
    ]

    assert resolve_terminal_failure_cause(attempts) is None
    assert not ladder_is_gate_decided(
        [
            (attempt.acceptance_decision, attempt.acceptance_reason)
            for attempt in attempts
        ]
    )


def test_a_provider_cause_over_a_provider_ladder_still_constructs() -> None:
    """The refusal is exactly scoped: a provider-decided terminal is untouched."""
    model = ModelDelegateSkillResponse.model_validate(
        {
            "correlation_id": str(uuid4()),
            "status": "failed",
            "task_type": "document",
            "quality_gate_passed": False,
            "quality_score": 0.0,
            "terminal_failure_cause": "provider_quota_exhausted",
            "attempts": [
                _rung(
                    decision=_CLIMB,
                    reason=_CALL_FAILED,
                    error_message="HTTP 429 quota exceeded",
                ).model_dump(mode="json")
            ],
        }
    )

    assert (
        model.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
    )


def test_a_success_is_never_given_the_gate_cause() -> None:
    """Control: the completed variant stays cause-free."""
    model = ModelDelegateSkillCompleted.model_validate(
        {
            "correlation_id": str(uuid4()),
            "task_type": "document",
            "quality_gate_passed": True,
            "quality_score": 1.0,
            "attempts": [
                _rung(
                    decision=EnumDelegationAcceptanceDecision.ACCEPT,
                    reason=EnumDelegationAcceptanceReason.QUALITY_BAR_MET,
                    passed=True,
                ).model_dump(mode="json")
            ],
        }
    )

    assert model.terminal_failure_cause is None


# ---------------------------------------------------------------------------
# The producer: the workflow terminal names the deciding event too.
# ---------------------------------------------------------------------------


def _workflow_request(cid: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
    )


def _workflow_routing(cid: UUID, tier_name: str) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type="test",
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{tier_name}"),
        endpoint_url="http://model-under-test.invalid:8000",
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale=f"Task 'test' routed via tier '{tier_name}'.",
        tier_name=tier_name,
    )


def _no_further_rung(
    workflow: DelegationWorkflowState, **_kwargs: object
) -> ModelEscalationDecisionResult:
    del workflow
    return ModelEscalationDecisionResult(
        can_escalate=False, terminal_failure_reason="fixture_no_higher_tier"
    )


def test_a_gate_exhausted_workflow_terminal_names_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: the gate-exhausted terminal carried no cause at all.

    The delegate-skill terminal built from it then read the gate's refusal text
    as a provider fault, which is exactly how ``73aba966`` came to say
    ``provider_error``.
    """
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_workflow_request(cid))
    handler.handle_routing_decision(_workflow_routing(cid, "claude"))
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="weak draft",
            model_used="qwen3-coder-30b",
            latency_ms=10,
        )
    )
    monkeypatch.setattr(handler, "_maybe_retry_local", lambda *_a, **_k: None)
    monkeypatch.setattr(handler, "_decide_escalation", _no_further_rung)

    events = handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=cid,
            passed=False,
            quality_score=0.2,
            failure_reasons=("TASK_MISMATCH: weak draft",),
            fallback_recommended=True,
        )
    )

    failed = [event for event in events if isinstance(event, ModelDelegationFailed)]
    assert len(failed) == 1
    assert failed[0].terminal_failure_cause is _GATE
    # The v2 terminal, when one is emitted, carries the gate arm and never the
    # provider arm, which core refuses to hold a gate cause.
    for event in events:
        cause = getattr(event, "routed_failure_cause", None)
        if cause is not None:
            assert cause.kind == "quality_gate_rejection"


def _gate_refused_rung(tier_name: str) -> ModelDelegationEscalationAttempt:
    return ModelDelegationEscalationAttempt(
        tier_name=tier_name,
        model_used="qwen3-coder-30b",
        quality_score=0.8,
        failure_reasons=("MALFORMED: unsupported deterministic DoD check",),
        latency_ms=10,
        fallback_recommended=True,
        attempted_at=datetime.now(UTC),
        acceptance_decision=EnumDelegationAcceptanceDecision.CLIMB,
        acceptance_reason=EnumDelegationAcceptanceReason.DETERMINISTIC_FLOOR_FAILED,
    )


def test_a_final_429_after_gate_refusals_names_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: the ``6ce51f77`` shape, a real 429 on the last rung of a gate-refused ladder.

    Pre-fix the terminal named the whole run quota-exhausted, so the over-quota
    metric counted capacity pressure for a run the gate had already decided.
    """
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_workflow_request(cid))
    handler.handle_routing_decision(_workflow_routing(cid, "claude"))
    handler.workflows[cid].escalation_history.extend(
        [_gate_refused_rung("local") for _ in range(3)]
    )
    monkeypatch.setattr(handler, "_maybe_retry_sibling_backend", lambda *_a, **_k: None)

    events = handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="",
            model_used="qwen3-coder-30b",
            latency_ms=10,
            error_message="429 RESOURCE_EXHAUSTED: provider quota exhausted",
        )
    )

    failed = [event for event in events if isinstance(event, ModelDelegationFailed)]
    assert len(failed) == 1
    assert failed[0].terminal_failure_cause is _GATE
    # The 429 is not erased: it is the run's own failure reason.
    assert failed[0].failure_reason.startswith("429 RESOURCE_EXHAUSTED")


def test_a_final_429_with_no_gate_refusal_still_names_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control on the producer: a provider-decided run keeps quota."""
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_workflow_request(cid))
    handler.handle_routing_decision(_workflow_routing(cid, "claude"))
    monkeypatch.setattr(handler, "_maybe_retry_sibling_backend", lambda *_a, **_k: None)

    events = handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="",
            model_used="qwen3-coder-30b",
            latency_ms=10,
            error_message="429 RESOURCE_EXHAUSTED: provider quota exhausted",
        )
    )

    failed = [event for event in events if isinstance(event, ModelDelegationFailed)]
    assert len(failed) == 1
    assert (
        failed[0].terminal_failure_cause
        is EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
    )
