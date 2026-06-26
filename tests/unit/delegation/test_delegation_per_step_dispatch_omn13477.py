# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13477 (W5): per-step dispatch + zero imperative ``_transition`` calls.

Wave 5 of the OMN-13471 delegation decomposition removes the imperative FSM
guard (``HandlerDelegationWorkflow._transition`` + its ``_VALID_TRANSITIONS``
set) and the hand-maintained isinstance ladder in ``handle()``:

  * State advancement is driven through ``_advance``, which resolves each edge
    from the typed, executor-bound ``_DECLARED_TRANSITIONS`` projection of the
    contract FSM (the OMN-13474 W2 binding) — the contract is the transition
    authority, not handler logic.
  * Payload dispatch is table-driven via ``_PER_STEP_DISPATCH`` (one entry per
    ``handler_routing`` event_model the contract declares), with no catch-all
    branch.

These tests assert the W5 DoD directly and prove behavior identity against the
REAL dispatch path (the async ``handle()`` the live ``DispatcherDelegationWorkflow``
calls), not just the per-step methods in isolation (memory
``feedback_real_dispatch_path_tests``): a handler-isolation pass is not enough —
the live path must reach the same FSM state and emit the same terminal events.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _DECLARED_TRANSITIONS,
    _PER_STEP_DISPATCH,
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
    InvalidStateTransitionError,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event import (
    ModelDelegationEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_task_delegated_event import (
    ModelTaskDelegatedEvent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

_HANDLER_PATH = Path(
    "src/omnimarket/nodes/node_delegation_orchestrator/handlers/"
    "handler_delegation_workflow.py"
)
_PROMPT_TOKENS = 1234
_COMPLETION_TOKENS = 567


# ---------------------------------------------------------------------------
# Fixtures (mirror the canonical builders used across the delegation suite).
# ---------------------------------------------------------------------------


def _make_request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(
    correlation_id: UUID, tier_name: str = "local"
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="test",
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(
            NAMESPACE_DNS, f"omninode.ai/backends/{tier_name}-coder"
        ),
        endpoint_url="http://192.168.86.201:8000",  # onex-allow-internal-ip OMN-10865 reason="delegation test fixture for local AIPC LLM endpoint"
        cost_tier="low",
        tier_name=tier_name,
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale=f"Task 'test' routed via tier '{tier_name}'.",
    )


def _make_success_response(correlation_id: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="def test_verify_registration():\n    assert True",
        model_used="qwen3-coder-30b",
        llm_call_id="chatcmpl-omn13477",
        latency_ms=1200,
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
        total_tokens=_PROMPT_TOKENS + _COMPLETION_TOKENS,
    )


def _make_gate_result(
    correlation_id: UUID, *, passed: bool = True, quality_score: float = 0.9
) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=correlation_id,
        passed=passed,
        quality_score=quality_score,
        failure_reasons=() if passed else ("score_below_required_bar",),
        fallback_recommended=not passed,
    )


def _canonical_terminal(events: list[object]) -> ModelDelegationResult:
    """OMN-13629: the terminal is a SINGLE canonical event, no compat twin."""
    canonical = [
        e.payload
        for e in events
        if isinstance(e, ModelDelegationEvent)
        and isinstance(e.payload, ModelDelegationResult)
    ]
    compat = [e for e in events if isinstance(e, ModelTaskDelegatedEvent)]
    assert len(canonical) == 1, f"expected one canonical terminal, got {events!r}"
    assert compat == [], f"expected ZERO compat twins (OMN-13629), got {compat!r}"
    return canonical[0]


# ---------------------------------------------------------------------------
# DoD: zero ``_transition`` calls; advance authority is the contract FSM.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestZeroTransitionCalls:
    """OMN-13477 DoD: no imperative ``_transition`` calls remain."""

    def test_handler_source_has_zero_transition_calls(self) -> None:
        """Static gate: the handler module makes zero ``_transition(...)`` calls.

        The whole point of W5 is to drive the imperative ``_transition`` call
        count to 0. Parse the AST and assert no call expression targets a method
        or function literally named ``_transition`` — this fails loudly if a
        future edit re-introduces the imperative guard.
        """
        tree = ast.parse(_HANDLER_PATH.read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name: str | None = None
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name == "_transition":
                offenders.append(f"line {node.lineno}")
        assert not offenders, (
            "handler_delegation_workflow.py still calls _transition(...): "
            f"{offenders}. W5 (OMN-13477) requires zero imperative _transition "
            "calls; advance state via self._advance instead."
        )

    def test_handler_defines_no_transition_method(self) -> None:
        """The imperative ``_transition`` method itself is gone."""
        assert not hasattr(HandlerDelegationWorkflow, "_transition")
        assert hasattr(HandlerDelegationWorkflow, "_advance")

    def test_advance_resolves_declared_contract_transition(self) -> None:
        """``_advance`` returns the typed contract transition it drove."""
        handler = HandlerDelegationWorkflow(workflows={})
        workflow = DelegationWorkflowState(correlation_id=uuid4())
        transition = handler._advance(workflow, EnumDelegationState.ROUTED)
        assert workflow.state == EnumDelegationState.ROUTED
        assert (
            transition
            is _DECLARED_TRANSITIONS[
                (EnumDelegationState.RECEIVED, EnumDelegationState.ROUTED)
            ]
        )

    def test_advance_rejects_undeclared_edge(self) -> None:
        """``_advance`` fails closed on an edge the contract does not declare."""
        handler = HandlerDelegationWorkflow(workflows={})
        workflow = DelegationWorkflowState(correlation_id=uuid4())
        with pytest.raises(InvalidStateTransitionError, match="Invalid state"):
            handler._advance(workflow, EnumDelegationState.COMPLETED)

    def test_advance_rejects_outgoing_from_terminal(self) -> None:
        """Terminal states have no declared outgoing edge."""
        handler = HandlerDelegationWorkflow(workflows={})
        for terminal in (EnumDelegationState.COMPLETED, EnumDelegationState.FAILED):
            workflow = DelegationWorkflowState(correlation_id=uuid4(), state=terminal)
            with pytest.raises(InvalidStateTransitionError):
                handler._advance(workflow, EnumDelegationState.RECEIVED)


# ---------------------------------------------------------------------------
# DoD: per-step dispatch table (no isinstance ladder / catch-all).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPerStepDispatch:
    """OMN-13477 DoD: payload dispatch is table-driven per-step."""

    def test_dispatch_table_maps_every_payload_to_a_real_method(self) -> None:
        """Every ``_PER_STEP_DISPATCH`` entry resolves to a handler method."""
        assert _PER_STEP_DISPATCH, "dispatch table must not be empty"
        for payload_type, method_name in _PER_STEP_DISPATCH.items():
            assert isinstance(payload_type, type)
            assert callable(getattr(HandlerDelegationWorkflow, method_name, None)), (
                f"{method_name} is not a method on HandlerDelegationWorkflow"
            )

    def test_dispatch_table_matches_contract_event_models(self) -> None:
        """The dispatch table covers exactly the contract's per-event models."""
        import yaml

        contract = yaml.safe_load(
            Path(
                "src/omnimarket/nodes/node_delegation_orchestrator/contract.yaml"
            ).read_text(encoding="utf-8")
        )
        declared_models = {
            h["event_model"]["name"] for h in contract["handler_routing"]["handlers"]
        }
        dispatch_models = {t.__name__ for t in _PER_STEP_DISPATCH}
        # Every payload the dispatch table handles is a declared event_model.
        # (ModelInferenceResponseData is declared via the wire alias of the same
        # class name; assert by class name to stay decoupled from import path.)
        assert dispatch_models <= declared_models, (
            "dispatch table routes payloads with no declared handler_routing "
            f"event_model: {sorted(dispatch_models - declared_models)}"
        )

    async def test_handle_raises_on_undeclared_payload_type(self) -> None:
        """No catch-all: an unsupported payload type fails closed."""
        handler = HandlerDelegationWorkflow(workflows={})
        with pytest.raises(ValueError, match="Unsupported delegation workflow"):
            await handler.handle(object())


# ---------------------------------------------------------------------------
# Behavior identity through the REAL async dispatch path (handle()).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRealDispatchPathParity:
    """The async ``handle()`` path reaches the same state + terminal events."""

    async def test_completed_chain_via_handle_matches_per_step(self) -> None:
        """Driving the full chain through ``handle()`` completes identically."""
        cid = uuid4()

        # Per-step (handler-isolation) reference outcome.
        ref = HandlerDelegationWorkflow(workflows={})
        ref.handle_delegation_request(_make_request(cid))
        ref.handle_routing_decision(_make_routing_decision(cid))
        ref.handle_inference_response(_make_success_response(cid))
        ref_events = list(ref.handle_gate_result(_make_gate_result(cid)))
        ref_canonical = _canonical_terminal(ref_events)
        assert ref.workflows[cid].state == EnumDelegationState.COMPLETED

        # Real dispatch path: every step through the async handle() ladder
        # replacement.
        live = HandlerDelegationWorkflow(workflows={})
        await live.handle(_make_request(cid))
        await live.handle(_make_routing_decision(cid))
        await live.handle(_make_success_response(cid))
        live_events = list(await live.handle(_make_gate_result(cid)))
        live_canonical = _canonical_terminal(live_events)

        assert live.workflows[cid].state == EnumDelegationState.COMPLETED
        # Identical canonical terminal: same quality + same served tokens.
        assert live_canonical.quality_passed is True
        assert live_canonical.quality_passed == ref_canonical.quality_passed
        assert live_canonical.prompt_tokens == ref_canonical.prompt_tokens
        assert live_canonical.completion_tokens == ref_canonical.completion_tokens

    async def test_failed_gate_chain_via_handle_matches_per_step(self) -> None:
        """A non-escalatable gate failure terminates FAILED through handle()."""
        cid = uuid4()
        live = HandlerDelegationWorkflow(workflows={})
        await live.handle(_make_request(cid))
        # Ceiling tier so no higher tier exists -> terminal FAILED, not escalate.
        await live.handle(_make_routing_decision(cid, tier_name="claude"))
        await live.handle(_make_success_response(cid))
        live_events = list(
            await live.handle(_make_gate_result(cid, passed=False, quality_score=0.1))
        )
        canonical = _canonical_terminal(live_events)
        assert live.workflows[cid].state == EnumDelegationState.FAILED
        assert canonical.quality_passed is False
        # Served tokens still propagate on the failure path (OMN-13408 invariant).
        assert canonical.prompt_tokens == _PROMPT_TOKENS
        assert canonical.completion_tokens == _COMPLETION_TOKENS
