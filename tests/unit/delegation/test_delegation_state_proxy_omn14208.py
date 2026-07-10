# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Tests for the OMN-14208 ContextVar-backed durable state proxy.

Covers:
- Fallback: ContextVar unset -> DelegationWorkflowStateProxy forwards to the
  process-wide ClassVar dict (byte-for-byte the pre-OMN-14208 behavior).
- Bound: omnibase_infra's CONTEXTVAR_STATE_IO_ROWS set -> proxy
  decode-once-and-caches from the bound raw JSON, and an in-place attribute
  mutation on the returned object is visible without a `__setitem__` call.
- Tenant recovery: a cold-process reload (a fresh HandlerDelegationWorkflow /
  fresh proxy, never touched by this correlation_id before) that loads a
  durably-persisted row carrying `tenant_id` must carry that SAME tenant onto
  the terminal event, not the shared 'omninode' default.
- Re-fold determinism: replaying an event against a persisted
  `inference_intent_in_flight=True` flag emits nothing (the synchronous
  in-flight dedup guard survives a cold-process reload).
- Flush bridge: `StateIoCodec.flush(cid)` — the pair-verify M1 bridge
  omnibase_infra's wiring calls post-handle — round-trips tenant_id/state/
  in_flight correctly across real handle() legs (pair-proof regression, M2).

Since OMN-14208 pair-verify M1, the proxy bridges DIRECTLY to
omnibase_infra's own `CONTEXTVAR_STATE_IO_ROWS` (the old market-local
`_DELEGATION_STATE_CONTEXT` / `bind_state_context` were retired). This
repo's pinned `omnibase-infra` rev predates that symbol (state_io ships in
omnibase_infra first, OMN-14208), so `_bind_infra_state_io_rows` below
injects a stand-in module exposing a real `ContextVar` of the same name
when the genuine import fails — the exact `ImportError` fallback path
`state_codec._read_active_rows` is built to tolerate. Once the infra pin is
bumped past the OMN-14208 release, the real import succeeds and these tests
exercise the genuine object with no changes needed here.

Related:
    - OMN-14208: durable per-request delegation FSM state
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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

_STATE_STORE_ADAPTER_MODULE = "omnibase_infra.runtime.state_io.state_store_adapter"


@contextmanager
def _bind_infra_state_io_rows(
    rows: dict[str, tuple[str | None, int]],
) -> Iterator[None]:
    """Bind a real ``CONTEXTVAR_STATE_IO_ROWS``-shaped ContextVar for the block.

    Injects a stand-in module at ``sys.modules`` if the genuine
    ``omnibase_infra.runtime.state_io.state_store_adapter`` import fails (the
    pinned ``omnibase-infra`` rev in this repo predates the OMN-14208 module —
    see ``state_codec._read_active_rows``). Restores whatever was previously
    at that ``sys.modules`` key afterward, so this never leaks across tests.
    """
    previous = sys.modules.get(_STATE_STORE_ADAPTER_MODULE)
    try:
        from omnibase_infra.runtime.state_io import state_store_adapter

        contextvar = state_store_adapter.CONTEXTVAR_STATE_IO_ROWS
    except ImportError:
        contextvar = ContextVar("onex_state_io_rows", default=None)
        fake_module = types.ModuleType(_STATE_STORE_ADAPTER_MODULE)
        fake_module.CONTEXTVAR_STATE_IO_ROWS = contextvar  # type: ignore[attr-defined]
        sys.modules[_STATE_STORE_ADAPTER_MODULE] = fake_module

    token = contextvar.set(rows)
    try:
        yield
    finally:
        contextvar.reset(token)
        if previous is None:
            sys.modules.pop(_STATE_STORE_ADAPTER_MODULE, None)
        else:
            sys.modules[_STATE_STORE_ADAPTER_MODULE] = previous


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
        payload_json = state_codec.encode(state).decode("utf-8")

        handler = HandlerDelegationWorkflow()
        with _bind_infra_state_io_rows({str(cid): (payload_json, 0)}):
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

            # Evict so this cid doesn't linger in the shared default proxy's
            # cache beyond this test (the proxy is a process-wide singleton,
            # OMN-14208 pair-verify M1).
            handler.workflows.flush(cid)  # type: ignore[attr-defined]

    def test_flush_reencodes_and_evicts(self) -> None:
        cid = uuid4()
        state = DelegationWorkflowState(
            correlation_id=cid,
            state=EnumDelegationState.RECEIVED,
            request=_make_request(cid),
        )
        payload_json = state_codec.encode(state).decode("utf-8")

        handler = HandlerDelegationWorkflow()
        with _bind_infra_state_io_rows({str(cid): (payload_json, 0)}):
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
        payload_json = state_codec.encode(persisted_state).decode("utf-8")

        # A brand-new handler: nothing in this process has ever touched `cid`
        # before — simulates the cold-process replay this design closes (the
        # shared default proxy's cache has no entry for this fresh random
        # cid regardless of process-lifetime sharing, OMN-14208 pair-verify
        # M1).
        handler = HandlerDelegationWorkflow()
        with _bind_infra_state_io_rows({str(cid): (payload_json, 0)}):
            events = handler.handle_gate_result(
                ModelQualityGateResult(
                    correlation_id=cid,  # type: ignore[arg-type]
                    passed=True,
                    quality_score=0.9,
                )
            )
            handler.workflows.flush(cid)  # type: ignore[attr-defined]

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
        payload_json = state_codec.encode(persisted_state).decode("utf-8")

        handler = HandlerDelegationWorkflow()
        with _bind_infra_state_io_rows({str(cid): (payload_json, 0)}):
            events = handler.handle_routing_decision(_make_routing_decision(cid))
            handler.workflows.flush(cid)  # type: ignore[attr-defined]

        assert events == []


@pytest.mark.unit
class TestStateIoCodecFlushBridge:
    """``StateIoCodec.flush(cid)`` is the pair-verify M1 bridge omnibase_infra's
    wiring calls post-handle (handler_wiring.py ``_load_handle_persist``)
    instead of reading a write-back value out of ``CONTEXTVAR_STATE_IO_ROWS``
    itself. This drives two REAL handle() legs against the shared default
    proxy and asserts the flushed JSON round-trips tenant_id/state/in_flight
    correctly — the exact guard the M2 fix (well-known ``in_flight`` key)
    needed and the individually-green infra/market suites lacked before the
    pair-verify (OMN-14208).
    """

    def test_flush_round_trips_tenant_state_and_in_flight_through_real_handle_legs(
        self,
    ) -> None:
        """Two SEPARATE load/handle/flush cycles — one per leg — mirroring
        exactly how omnibase_infra's wiring drives each leg as its own
        dispatch: fresh ``adapter.load(cid)`` (here, a fresh
        ``_bind_infra_state_io_rows`` bind of whatever the prior leg
        "persisted"), run ``handle()``, then ``codec.flush(cid)``. Asserts
        every one of the 3 seam fields (``tenant_id``/``state``/``in_flight``)
        by NAME and VALUE at each leg — including the ``in_flight``
        False -> True transition — not just presence of the keys (this is
        the seam-match regression guard, OMN-14208 pair-verify).
        """
        cid = uuid4()
        request = _make_request(cid, tenant_id="acme-corp")
        routing_decision = _make_routing_decision(cid)

        handler = HandlerDelegationWorkflow()
        codec = state_codec.StateIoCodec()

        # Leg 1: no row yet -> creates the workflow (in_flight=False,
        # state=RECEIVED).
        with _bind_infra_state_io_rows({str(cid): (None, 0)}):
            handler.handle_delegation_request(request)
            leg1_flushed = codec.flush(str(cid))

        assert leg1_flushed is not None
        leg1_parsed = json.loads(leg1_flushed)
        assert leg1_parsed["tenant_id"] == "acme-corp"
        assert leg1_parsed["state"] == EnumDelegationState.RECEIVED.value
        assert leg1_parsed["in_flight"] is False

        leg1_decoded = state_codec.decode(leg1_flushed)
        assert leg1_decoded.tenant_id == "acme-corp"
        assert leg1_decoded.state == EnumDelegationState.RECEIVED
        assert leg1_decoded.inference_intent_in_flight is False

        # Leg 2: loads leg 1's "persisted" row fresh (a new bind, exactly
        # like a real second dispatch reloading from the DB) -> RECEIVED ->
        # ROUTED, sets inference_intent_in_flight=True.
        with _bind_infra_state_io_rows({str(cid): (leg1_flushed, 0)}):
            handler.handle_routing_decision(routing_decision)
            leg2_flushed = codec.flush(str(cid))

        assert leg2_flushed is not None
        leg2_parsed = json.loads(leg2_flushed)
        assert leg2_parsed["tenant_id"] == "acme-corp"
        assert leg2_parsed["state"] == EnumDelegationState.ROUTED.value
        assert leg2_parsed["in_flight"] is True

        leg2_decoded = state_codec.decode(leg2_flushed)
        assert leg2_decoded.tenant_id == "acme-corp"
        assert leg2_decoded.state == EnumDelegationState.ROUTED
        assert leg2_decoded.inference_intent_in_flight is True

    def test_flush_returns_none_when_untouched(self) -> None:
        """A dispatch that never loads/sets `cid` on the shared proxy this
        request must return None from flush -- infra then skips persistence
        (matches _load_handle_persist's "nothing changed" no-op-skip path)."""
        cid = uuid4()
        codec = state_codec.StateIoCodec()
        with _bind_infra_state_io_rows({str(cid): (None, 0)}):
            assert codec.flush(str(cid)) is None
