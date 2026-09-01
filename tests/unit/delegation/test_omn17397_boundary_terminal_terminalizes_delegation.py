# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17397 — a DLQ'd config failure must still close the caller's workflow.

OMN-16812 landed two halves of one answer and shipped only the first. The
consume boundary in ``omnibase_infra`` now terminalizes a handler failure it is
about to ACK (``handler_wiring._emit_boundary_failure_terminal``), and
``node_delegation_routing_reducer/contract.yaml`` declares the address it
answers at (``onex.evt.omnibase-infra.routing-decision-failed.v1``). That
contract says so itself, in the comment shipped beside the declaration:

    Propagating this terminal through the delegation FSM to
    ``delegation-failed.v1`` is the remaining half of OMN-16812 AC1 and is
    deliberately not smuggled in here.

Nothing propagated it. A cross-repo grep for ``routing-decision-failed`` across
omnimarket, omnibase_infra, omnibase_core, omninode_infra and omnibase_compat
returns the reducer's own declaration and one test — **zero subscribers**. The
delegation orchestrator subscribes to ``routing-decision.v1`` alone, so a
routing raise leaves its FSM parked in ``RECEIVED`` forever,
``delegation-failed.v1`` is never published, and ``onex-api``'s
``workflow_terminal_consumer`` (the only writer of ``gateway_workflows.status``)
never fires.

That is the live defect, staging ``Deploy onex-staging`` run ``33443670050``,
correlation ``e317122c-13b2-4983-988b-1050065d2929``::

    22:01:57 [ERROR] handler_wiring: HandlerDispatchFailureError: dispatch to
      topic=onex.cmd.omnibase-infra.delegation-routing-request.v1
      returned status=handler_error with no terminal output
      ProtocolConfigurationError: [ONEX_CORE_041_INVALID_CONFIGURATION]
      No tier has a configured endpoint for task_type='summarization'.
    22:01:57 [ERROR] metric_name=boundary_swallow_prevented dlq_routed=true

    -- 30 minutes later --
    status = published    completed_at = NULL

The record was safe. The caller was not told. This module pins the missing hop.

Four bands, escalating:

1. **Declaration** — the orchestrator contract must subscribe to the terminal,
   route it to the workflow handler, and declare the ``RECEIVED -> FAILED`` FSM
   edge a routing failure needs. Read from the REAL contract file, never a
   transcription: a transcription is the drift that produced "declared, and
   still inert" the first time.
2. **The real dispatch path** — driven through the async ``handle()`` the live
   ``DispatcherDelegationWorkflow`` calls, not the per-step method in isolation
   (memory ``feedback_real_dispatch_path_tests``).
3. **Idempotency** — Kafka is at-least-once and the DLQ leg can redeliver. Two
   deliveries of one terminal must produce ONE ``ModelDelegationFailed``, and a
   terminal that does not describe what the workflow is waiting on must not
   mint a second one.
4. **The whole chain, real wiring** — ``wire_from_manifest`` with a real
   ``MessageDispatchEngine``, a real ``EventBusInmemory`` and the REAL
   ``HandlerRoutingIntent`` raising the REAL ``ProtocolConfigurationError``, so
   the terminal fed to the orchestrator is the one the boundary actually
   published rather than one this test invented.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.boundary_failure_terminal import (
    ModelBoundaryFailureTerminal,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _DECLARED_TRANSITIONS,
    _PER_STEP_DISPATCH,
    HandlerDelegationWorkflow,
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

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NODES = _REPO_ROOT / "src" / "omnimarket" / "nodes"
_ORCHESTRATOR_CONTRACT = _NODES / "node_delegation_orchestrator" / "contract.yaml"
_REDUCER_DIR = _NODES / "node_delegation_routing_reducer"
_REDUCER_CONTRACT = _REDUCER_DIR / "contract.yaml"
_PROJECTION_CONTRACT = _NODES / "node_projection_delegation" / "contract.yaml"

# Verbatim from the live incident trace (OMN-17397 ticket body, run 33443670050).
_ROUTING_REQUEST_TOPIC = "onex.cmd.omnibase-infra.delegation-routing-request.v1"  # onex-topic-allow: verbatim from the live incident trace
_ROUTING_FAILED_TOPIC = "onex.evt.omnibase-infra.routing-decision-failed.v1"  # onex-topic-allow: the terminal OMN-16812 declared and nobody consumed
_DELEGATION_FAILED_TOPIC = "onex.evt.omnibase-infra.delegation-failed.v1"  # onex-topic-allow: the only topic onex-api maps to gateway status 'failed'
_LIVE_ONEX_CODE = "ONEX_CORE_041_INVALID_CONFIGURATION"
_LIVE_FAILURE_CLASS = "ProtocolConfigurationError"
_LIVE_FAILURE_REASON = (
    f"{_LIVE_FAILURE_CLASS}: [{_LIVE_ONEX_CODE}] No tier has a configured "
    "endpoint for task_type='summarization'"
)


def _raw(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} is not a YAML mapping"
    return loaded


def _make_request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Reply with the single word: alive.",
        task_type="research",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _boundary_terminal(correlation_id: UUID) -> ModelBoundaryFailureTerminal:
    """The terminal the boundary publishes, in the live incident's shape."""
    return ModelBoundaryFailureTerminal(
        correlation_id=correlation_id,
        failure_class=_LIVE_FAILURE_CLASS,
        failure_code=_LIVE_ONEX_CODE,
        retryable=False,
        failure_reason=_LIVE_FAILURE_REASON,
        origin_topic=_ROUTING_REQUEST_TOPIC,
    )


def _make_decision(correlation_id: UUID) -> ModelRoutingDecision:
    """A routing decision in the canonical shape the suite already uses."""
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="research",
        selected_model="local/routing-test-model",
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/local-coder"),
        endpoint_url="http://test-coder:8000",
        cost_tier="low",
        tier_name="free_local",
        max_context_tokens=65536,
        max_tokens=32,
        system_prompt="You are a test assistant.",
        rationale="Task 'research' routed via tier 'free_local'.",
    )


# ---------------------------------------------------------------------------
# 1 — declaration: the terminal has an addressee in the orchestrator
# ---------------------------------------------------------------------------


def test_orchestrator_subscribes_to_the_routing_failure_terminal() -> None:
    """RED before this ticket: the topic had a publisher and no subscriber.

    The boundary emits onto the reducer's declared failure terminal; if the
    orchestrator does not consume it, the emission is a second silent drop and
    the caller's row never leaves ``published``.
    """
    reducer_publishes = _raw(_REDUCER_CONTRACT)["event_bus"]["publish_topics"]
    assert _ROUTING_FAILED_TOPIC in reducer_publishes, (
        "precondition: OMN-16812 declared this terminal on the reducer"
    )

    subscribes = _raw(_ORCHESTRATOR_CONTRACT)["event_bus"]["subscribe_topics"]
    assert _ROUTING_FAILED_TOPIC in subscribes, (
        "the routing reducer's failure terminal has a publisher and no "
        "subscriber — this is the stall: the delegation FSM never learns the "
        "routing leg died, so delegation-failed.v1 is never published and "
        "gateway_workflows.status stays 'published' forever"
    )


def test_orchestrator_routes_the_boundary_terminal_to_the_workflow_handler() -> None:
    """A subscription with no ``handler_routing`` entry dispatches to nothing.

    The entry is asserted whole — topic binding, event_model and handler — so a
    half-declaration (subscribed but unrouted) cannot pass as a fix.
    """
    routing = _raw(_ORCHESTRATOR_CONTRACT)["handler_routing"]
    entries = [
        entry
        for entry in routing["handlers"]
        if entry.get("topic") == _ROUTING_FAILED_TOPIC
    ]
    assert len(entries) == 1, (
        f"expected exactly one handler_routing entry bound to "
        f"{_ROUTING_FAILED_TOPIC}; found {len(entries)}"
    )
    entry = entries[0]
    assert entry["handler"]["name"] == "HandlerDelegationWorkflow"
    assert entry["event_model"]["name"] == "ModelBoundaryFailureTerminal"
    assert (
        entry["event_model"]["module"]
        == "omnibase_infra.runtime.boundary_failure_terminal"
    ), (
        "the event_model must name the SAME class the boundary publishes — a "
        "repo-local copy of the wire shape is exactly the drift that lets a "
        "field rename re-open this stall silently"
    )


def test_per_step_dispatch_covers_the_boundary_terminal() -> None:
    """``handle()`` fails closed on an undeclared payload type — cover this one."""
    assert ModelBoundaryFailureTerminal in _PER_STEP_DISPATCH, (
        "HandlerDelegationWorkflow.handle raises ValueError for any payload "
        "type absent from _PER_STEP_DISPATCH; without an entry the terminal "
        "would arrive and immediately re-raise at the boundary"
    )


def test_fsm_declares_the_received_to_failed_edge() -> None:
    """A routing failure lands while the workflow is still in ``RECEIVED``.

    ``_advance`` resolves every edge from the contract FSM and rejects anything
    undeclared, so without this edge the handler could not terminalize the
    exact state the live incident was in.
    """
    assert (
        EnumDelegationState.RECEIVED,
        EnumDelegationState.FAILED,
    ) in _DECLARED_TRANSITIONS, (
        "the live stall was a workflow in RECEIVED (routing intent emitted, no "
        "decision ever returned); with no RECEIVED -> FAILED edge the contract "
        "FSM cannot express the outcome that actually happened"
    )
    # The ROUTED-origin edge already exists and covers the escalation re-route
    # whose fresh routing request fails; asserted so a contract edit cannot
    # quietly drop the half this fix depends on.
    assert (
        EnumDelegationState.ROUTED,
        EnumDelegationState.FAILED,
    ) in _DECLARED_TRANSITIONS


def test_the_terminal_topic_is_the_one_the_gateway_projects() -> None:
    """The emitted class must resolve to the topic ``onex-api`` maps to failed.

    ``workflow_terminal_consumer`` (omninode_infra ``docker/onex-api``) is the
    sole writer of ``gateway_workflows.status`` and keys off exactly two topic
    constants. Emitting a correct terminal onto any other topic would close
    nothing, so the class-name -> topic mapping is pinned here, and the
    delegation projection's own subscription is asserted alongside it: the
    terminal must have an addressee on BOTH the gateway and projection legs.
    """
    published = {
        entry["event_type"]: entry["topic"]
        for entry in _raw(_ORCHESTRATOR_CONTRACT)["published_events"]
    }
    assert published["DelegationFailed"] == _DELEGATION_FAILED_TOPIC

    projection_subscribes = _raw(_PROJECTION_CONTRACT)["event_bus"]["subscribe_topics"]
    assert _DELEGATION_FAILED_TOPIC in projection_subscribes


# ---------------------------------------------------------------------------
# 2 — the real dispatch path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boundary_terminal_terminalizes_a_workflow_in_received() -> None:
    """AC1/AC2 — one boundary terminal in, one attributed delegation terminal out.

    Driven through the async ``handle()`` the live dispatcher calls, so the
    per-step handler, the ``_PER_STEP_DISPATCH`` binding and the FSM edge are
    all exercised the way production exercises them.
    """
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow(workflows={})

    intents = await handler.handle(_make_request(correlation_id))
    assert len(intents) == 1, "the request must emit the routing intent first"
    assert handler.workflows[correlation_id].state == EnumDelegationState.RECEIVED, (
        "precondition: the live stall was a workflow parked in RECEIVED"
    )

    events = await handler.handle(_boundary_terminal(correlation_id))

    assert len(events) == 1, (
        "a routing-leg boundary failure must produce exactly one delegation "
        f"terminal; got {[type(e).__name__ for e in events]}"
    )
    terminal = events[0]
    assert isinstance(terminal, ModelDelegationResult)
    assert type(terminal).__name__ == "ModelDelegationFailed", (
        "the terminal CLASS is what resolves the publish topic — a "
        "ModelDelegationCompleted here would close the row as a success"
    )
    assert terminal.correlation_id == correlation_id
    # AC2: the caller is told the configuration class, not 'dispatch_timeout'.
    assert _LIVE_FAILURE_CLASS in terminal.failure_reason
    assert _LIVE_ONEX_CODE in terminal.failure_reason
    assert "timeout" not in terminal.failure_reason.casefold()
    assert terminal.terminal_failure_reason is not None
    assert _LIVE_FAILURE_CLASS in terminal.terminal_failure_reason
    assert terminal.quality_passed is False

    assert handler.workflows[correlation_id].state == EnumDelegationState.FAILED


@pytest.mark.asyncio
async def test_boundary_terminal_terminalizes_a_reroute_that_failed() -> None:
    """The escalation re-route publishes a fresh routing request; it can fail too.

    State is ``ROUTED`` with ``routing_decision`` reset to ``None`` — the shape
    every escalation / retry-local re-entry leaves behind while it waits for a
    new decision.
    """
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(_make_request(correlation_id))
    await handler.handle(_make_decision(correlation_id))
    workflow = handler.workflows[correlation_id]
    assert workflow.state == EnumDelegationState.ROUTED
    # The re-route reset: a new routing request is in flight, no decision yet.
    workflow.routing_decision = None
    workflow.inference_intent_in_flight = False

    events = await handler.handle(_boundary_terminal(correlation_id))

    assert len(events) == 1
    assert type(events[0]).__name__ == "ModelDelegationFailed"
    assert workflow.state == EnumDelegationState.FAILED


# ---------------------------------------------------------------------------
# 3 — idempotency: the DLQ leg stays, and must not race into a double terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redelivered_boundary_terminal_emits_exactly_one_terminal() -> None:
    """Kafka is at-least-once — correlation id is the idempotency key.

    A second ``ModelDelegationFailed`` for one correlation would double-write
    the delegation projection and re-close an already-closed gateway row.
    """
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(_make_request(correlation_id))

    first = await handler.handle(_boundary_terminal(correlation_id))
    second = await handler.handle(_boundary_terminal(correlation_id))

    assert len(first) == 1
    assert second == [], (
        "the workflow is already FAILED; a redelivered boundary terminal must "
        "be a no-op, not a second terminal event"
    )
    assert handler.workflows[correlation_id].state == EnumDelegationState.FAILED


@pytest.mark.asyncio
async def test_boundary_terminal_for_an_unknown_correlation_emits_nothing() -> None:
    """No workflow means no caller of ours is waiting — never mint one.

    Fabricating a terminal for a correlation this orchestrator never accepted
    would write a delegation row for a workflow that does not exist here.
    """
    handler = HandlerDelegationWorkflow(workflows={})
    assert await handler.handle(_boundary_terminal(uuid4())) == []


@pytest.mark.asyncio
async def test_boundary_terminal_after_a_live_routing_decision_is_ignored() -> None:
    """A terminal that does not describe what the workflow awaits is not a verdict.

    Once a routing decision has been accepted and inference is in flight, the
    workflow's outcome belongs to the inference/quality legs. Terminalizing on
    a stale or misordered routing failure here would race the live path into a
    second terminal for one correlation.
    """
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(_make_request(correlation_id))
    await handler.handle(_make_decision(correlation_id))
    workflow = handler.workflows[correlation_id]
    assert workflow.state == EnumDelegationState.ROUTED
    assert workflow.routing_decision is not None

    assert await handler.handle(_boundary_terminal(correlation_id)) == []
    assert workflow.state == EnumDelegationState.ROUTED


# ---------------------------------------------------------------------------
# 4 — the whole chain against real wiring
# ---------------------------------------------------------------------------

# A LOAD-ABLE bifrost contract whose only backends are ``tier: local``, so a
# ``claude`` tier floor leaves no eligible tier and the REAL reducer raises the
# REAL ProtocolConfigurationError (ONEX_CORE_041) — the live failure class,
# reached through real config resolution rather than a stand-in raise. Schema
# mirrors tests/unit/delegation/test_omn16812_reducer_failure_terminal.py; a
# contract that fails to LOAD is a different defect on a different path.
_LOCAL_ONLY_BIFROST = """
config_version: '2.0.0'
schema_version: bifrost_delegation.v1
backends:
  - backend_id: local-coder
    endpoint_url: "http://test-coder:8000"
    model_name: "local/routing-test-model"
    tier: local
    timeout_ms: 30000
    capabilities: [research]
routing_rules:
  - rule_id: "11111111-1111-4111-8111-111111111111"
    priority: 10
    task_class: research
    task_class_contract_version: "1.0.0"
    backend_policy_version: "2.0.0"
    match_operation_types: [chat_completion]
    match_capabilities: [research]
    backend_ids: [local-coder]
    fallback_policy:
      action: escalate_to_next_tier
      max_retries: 1
      on_exhaust: return_error
    shadow_policy_id: "22222222-2222-4222-8222-222222222222"
default_backends:
  - local-coder
circuit_breaker:
  failure_threshold: 5
  window_seconds: 30
failover:
  max_attempts: 3
  backoff_base_ms: 500
shadow_mode:
  enabled: false
  policy_version: "test"
  log_sample_rate: 1.0
  comparison_logging_enabled: true
  max_shadow_latency_ms: 5.0
"""


class _DlqRecordingInmemoryBus(EventBusInmemory):
    """In-memory bus honoring the boundary's duck-typed DLQ contract.

    ``EventBusInmemory`` has no ``_publish_raw_to_dlq``; ``EventBusKafka`` does,
    and that method is the only reason the boundary preserves the record at
    all. Publishing onto the topic's real DLQ address puts BOTH effects of one
    raise on the same observable bus.
    """

    async def _publish_raw_to_dlq(
        self,
        *,
        original_topic: str,
        raw_msg: object,
        error: Exception,
        correlation_id: UUID,
        failure_type: str,
        consumer_group: str,
        dlq_topic: str,
    ) -> bool:
        record = ModelEventEnvelope[object](
            payload={
                "original_topic": original_topic,
                "failure_type": failure_type,
                "error_type": type(error).__name__,
                "error": str(error),
            },
            correlation_id=correlation_id,
            event_type="omnibase-infra.dlq",
        )
        await self.publish(
            dlq_topic, None, record.model_dump_json().encode("utf-8"), None
        )
        return True


@pytest.fixture
def _local_only_bifrost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as routing

    original_config = routing._config
    routing._config = None
    routing._load_bifrost_endpoints.cache_clear()
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_LOCAL_ONLY_BIFROST, encoding="utf-8")
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    # OMN-14507's boundary DLQ is default-OFF behind this flag; the staging lane
    # runs with it ON (the incident's ``dlq_enabled=True`` line), and the
    # terminal emission is reached from the DLQ-persisted branch.
    monkeypatch.setenv("ONEX_BOUNDARY_DLQ_ENABLED", "true")
    yield
    routing._config = original_config
    routing._load_bifrost_endpoints.cache_clear()


def _routing_intent_wire(correlation_id: UUID) -> dict[str, object]:
    """``ModelRoutingIntent`` exactly as the orchestrator publishes it."""
    return {
        "intent": "routing_reducer",
        "payload": {
            "prompt": "Reply with the single word: alive.",
            "task_type": "research",
            "correlation_id": str(correlation_id),
            "max_tokens": 32,
            "emitted_at": datetime.now(UTC).isoformat(),
        },
        "min_tier_name": "claude",
        "excluded_backend_refs": [],
    }


async def _capture_real_boundary_terminal(
    correlation_id: UUID,
) -> ModelEventEnvelope[object] | None:
    """Drive one failing record through the REAL reducer wiring.

    Returns the envelope the boundary published onto the reducer's declared
    failure terminal — the artifact the orchestrator has to consume — or
    ``None`` if it never arrived.
    """
    manifest = discover_contracts_from_paths([_REDUCER_CONTRACT])
    assert not manifest.errors, f"contract discovery errored: {manifest.errors}"

    bus = _DlqRecordingInmemoryBus()
    await bus.start()
    seen: list[ModelEventEnvelope[object]] = []
    arrived = asyncio.Event()

    async def _collect(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[object].model_validate_json(message.value)
        if envelope.correlation_id == correlation_id:
            seen.append(envelope)
            arrived.set()

    await bus.subscribe(
        _ROUTING_FAILED_TOPIC, group_id="omn17397-terminal", on_message=_collect
    )

    engine = MessageDispatchEngine()
    await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    engine.freeze()

    command = ModelEventEnvelope[object](
        payload=_routing_intent_wire(correlation_id),
        correlation_id=correlation_id,
        event_type="omnibase-infra.delegation-routing-request",
    )
    await bus.publish(
        _ROUTING_REQUEST_TOPIC, None, command.model_dump_json().encode("utf-8"), None
    )
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(arrived.wait(), timeout=30)
    return seen[0] if seen else None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_local_only_bifrost")
async def test_the_boundary_terminal_the_runtime_publishes_closes_the_workflow() -> (
    None
):
    """AC1/AC3 end to end — the artifact under test is the real one.

    The reducer raises the real ``ProtocolConfigurationError`` through real
    wiring; the boundary publishes the real terminal; the orchestrator consumes
    that exact wire payload (re-validated into the contract-declared
    ``event_model``, as the runtime does) and answers with the delegation
    terminal the gateway projects. No hand-built terminal anywhere on this path.
    """
    correlation_id = uuid4()
    envelope = await _capture_real_boundary_terminal(correlation_id)
    assert envelope is not None, (
        "the boundary published no terminal on the reducer's declared failure "
        f"terminal {_ROUTING_FAILED_TOPIC}; OMN-16812's emitter is the "
        "precondition for this ticket's fix"
    )
    assert isinstance(envelope.payload, dict)
    # The runtime validates the wire dict into the contract-declared event_model
    # before calling handle(); do exactly that, so a field-name divergence
    # between publisher and consumer fails here rather than in staging.
    terminal = ModelBoundaryFailureTerminal.model_validate(envelope.payload)
    assert terminal.failure_class == _LIVE_FAILURE_CLASS
    assert terminal.failure_code == _LIVE_ONEX_CODE
    assert terminal.retryable is False

    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(_make_request(correlation_id))
    events = await handler.handle(terminal)

    assert len(events) == 1, (
        "the real boundary terminal did not close the workflow — this is the "
        "staging stall: status stays 'published', completed_at stays NULL"
    )
    delegation_terminal = events[0]
    assert type(delegation_terminal).__name__ == "ModelDelegationFailed"
    assert isinstance(delegation_terminal, ModelDelegationResult)
    assert delegation_terminal.correlation_id == correlation_id
    assert _LIVE_FAILURE_CLASS in delegation_terminal.failure_reason
    assert _LIVE_ONEX_CODE in delegation_terminal.failure_reason
    assert handler.workflows[correlation_id].state == EnumDelegationState.FAILED
