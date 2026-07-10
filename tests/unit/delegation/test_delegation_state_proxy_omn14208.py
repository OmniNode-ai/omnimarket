# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Tests for the OMN-14208 ContextVar-backed durable state proxy.

Covers:
- Fallback: ContextVar unset -> DelegationWorkflowStateProxy forwards to the
  process-wide ClassVar dict (byte-for-byte the pre-OMN-14208 behavior).
- Bound: ContextVar set -> proxy decode-once-and-caches from the bound raw
  JSON mapping, and an in-place attribute mutation on the returned object is
  visible without a `__setitem__` call.
- Tenant recovery: a cold-process reload (a fresh HandlerDelegationWorkflow /
  fresh proxy, never touched by this correlation_id before) that loads a
  durably-persisted row carrying `tenant_id` must carry that SAME tenant onto
  the terminal event, not the shared 'omninode' default.
- Re-fold determinism: replaying an event against a persisted
  `inference_intent_in_flight=True` flag emits nothing (the synchronous
  in-flight dedup guard survives a cold-process reload).

Related:
    - OMN-14208: durable per-request delegation FSM state
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_delegation_orchestrator import state_codec
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
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
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)


def _make_request(
    correlation_id: object, tenant_id: str | None = None
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",
        correlation_id=correlation_id,  # type: ignore[arg-type]
        emitted_at=datetime.now(UTC),
        tenant_id=tenant_id,
    )


def _make_routing_decision(correlation_id: object) -> ModelRoutingDecision:
    from uuid import NAMESPACE_DNS, uuid5

    return ModelRoutingDecision(
        correlation_id=correlation_id,  # type: ignore[arg-type]
        task_type="test",
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(
            NAMESPACE_DNS, "omninode.ai/backends/qwen3-coder-30b"
        ),
        endpoint_url="http://192.168.86.201:8000",  # onex-allow-internal-ip OMN-10865 reason="delegation test fixture for local AIPC LLM endpoint"
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale="Task 'test' routed to qwen3-coder-30b.",
        tier_name="local",
    )


@pytest.mark.unit
class TestProxyFallback:
    """ContextVar unset -> DelegationWorkflowStateProxy forwards to the
    ClassVar dict, matching pre-OMN-14208 HandlerDelegationWorkflow() behavior.
    """

    def test_default_construction_shares_classvar_across_instances(self) -> None:
        cid = uuid4()
        request_handler = HandlerDelegationWorkflow()
        routing_handler = HandlerDelegationWorkflow()

        request_handler.handle_delegation_request(_make_request(cid))
        # A second, independently-constructed handler instance must see the
        # same workflow via the shared ClassVar fallback (no ContextVar bound).
        assert cid in routing_handler.workflows
        assert routing_handler.workflows[cid].state == EnumDelegationState.RECEIVED

    def test_proxy_len_iter_contain_match_classvar_when_unbound(self) -> None:
        cid = uuid4()
        handler = HandlerDelegationWorkflow()
        handler.handle_delegation_request(_make_request(cid))

        proxy = handler.workflows
        assert cid in proxy
        assert cid in list(proxy)
        assert len(proxy) == len(HandlerDelegationWorkflow.shared_workflows())


@pytest.mark.unit
class TestProxyBoundContext:
    """ContextVar bound -> decode-once-and-cache; in-place mutation visible."""

    def test_bound_context_decodes_and_caches(self) -> None:
        cid = uuid4()
        state = DelegationWorkflowState(
            correlation_id=cid,
            state=EnumDelegationState.RECEIVED,
            request=_make_request(cid),
        )
        raw = {cid: state_codec.encode(state)}

        handler = HandlerDelegationWorkflow()
        with state_codec.bind_state_context(raw):
            loaded_once = handler.workflows[cid]
            loaded_again = handler.workflows[cid]
            # Same cached object on a second read within the bound context —
            # not re-decoded from raw JSON each time.
            assert loaded_once is loaded_again

            # An in-place attribute mutation (never __setitem__) must be
            # visible through the same cached reference, mirroring the real
            # `workflow.inference_intent_in_flight = True` dedup-flag write.
            loaded_once.compliance_attempts = 7
            assert handler.workflows[cid].compliance_attempts == 7

    def test_flush_reencodes_and_evicts(self) -> None:
        cid = uuid4()
        state = DelegationWorkflowState(
            correlation_id=cid,
            state=EnumDelegationState.RECEIVED,
            request=_make_request(cid),
        )
        raw = {cid: state_codec.encode(state)}

        handler = HandlerDelegationWorkflow()
        with state_codec.bind_state_context(raw):
            handler.workflows[cid].compliance_attempts = 3
            proxy = handler.workflows
            flushed = proxy.flush(cid)  # type: ignore[attr-defined]
            decoded_back = state_codec.decode(flushed)
            assert decoded_back.compliance_attempts == 3

            # Evicted: a second flush of the same cid has nothing cached.
            with pytest.raises(KeyError):
                proxy.flush(cid)  # type: ignore[attr-defined]


@pytest.mark.unit
class TestTenantRecoveryOnColdReload:
    """A fresh handler/proxy loading a durably-persisted row must carry the
    row's own tenant_id onto the terminal event, never the shared default.
    """

    def test_leg3_gate_result_carries_persisted_tenant(self) -> None:
        cid = uuid4()
        request = _make_request(cid, tenant_id="tenant-xyz")
        routing_decision = _make_routing_decision(cid)

        persisted_state = DelegationWorkflowState(
            correlation_id=cid,
            state=EnumDelegationState.INFERENCE_COMPLETED,
            request=request,
            routing_decision=routing_decision,
            inference_content="def test_foo():\n    assert True",
            inference_model_used="qwen3-coder-30b",
            inference_prompt_tokens=10,
            inference_completion_tokens=20,
            inference_total_tokens=30,
            current_tier_name="local",
            tenant_id="tenant-xyz",
        )
        raw = {cid: state_codec.encode(persisted_state)}

        # A brand-new handler/proxy: nothing in this process has ever touched
        # `cid` before — simulates the cold-process replay this design closes.
        handler = HandlerDelegationWorkflow()
        with state_codec.bind_state_context(raw):
            events = handler.handle_gate_result(
                ModelQualityGateResult(
                    correlation_id=cid,  # type: ignore[arg-type]
                    passed=True,
                    quality_score=0.9,
                )
            )

        terminal = next(e for e in events if isinstance(e, ModelDelegationEvent))
        result: ModelDelegationResult = terminal.payload
        assert result.tenant_id == "tenant-xyz"
        assert result.tenant_id != "omninode"


@pytest.mark.unit
class TestRefoldDeterminism:
    """Replaying a routing-decision event against a persisted in-flight flag
    emits nothing — the synchronous dedup guard survives a cold-process reload.
    """

    def test_replayed_routing_decision_against_persisted_in_flight_flag(self) -> None:
        cid = uuid4()
        request = _make_request(cid)
        routing_decision = _make_routing_decision(cid)

        persisted_state = DelegationWorkflowState(
            correlation_id=cid,
            state=EnumDelegationState.ROUTED,
            request=request,
            routing_decision=routing_decision,
            inference_content=None,
            inference_intent_in_flight=True,
            current_tier_name="local",
        )
        raw = {cid: state_codec.encode(persisted_state)}

        handler = HandlerDelegationWorkflow()
        with state_codec.bind_state_context(raw):
            events = handler.handle_routing_decision(_make_routing_decision(cid))

        assert events == []
