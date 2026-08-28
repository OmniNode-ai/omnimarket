# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16812 AC1 residual — the reducer must declare the terminal it fails to.

omnibase_infra#2962 made the auto-wired consume boundary terminalize a handler
failure it is about to ACK. It is **inert for the node that produced the
incident**. The emitter's first gate is::

    if len(failure_terminal_topics) != 1:
        return

and ``failure_terminal_topics`` is
``_declared_failure_terminal_topics(contract, success_topic=...)`` — the
contract's OWN declared terminals, read through
``load_terminal_event_topics``, the same single reader the Pattern B broker's
subscription set is built from. ``node_delegation_routing_reducer`` declared
NO terminal events at all, so that gate returned zero, the emitter returned
early, and the ``.201`` dev-lane reproduction was unchanged by the merge: the
record was DLQ'd in milliseconds and the caller still waited out 120.0 s to be
told ``dispatch_timeout`` / ``retryable: true``.

This module pins the declaration at the seams that actually consume it, using
omnibase_infra's own readers against the REAL contract file — never a
transcription of it, because a transcription is exactly the drift that
produces "declared, and still inert".

Three claims, escalating:

1. :func:`test_reducer_declares_exactly_one_failure_terminal` — the emitter's
   own gate, evaluated on the real contract. RED before this ticket: the
   reader returned ``()``.
2. :func:`test_failure_terminal_is_awaited_by_the_local_ingress_route` — the
   terminal has an addressee. A failure published where nothing waits is a
   second silent drop, so the local-ingress route (the Pattern B broker's
   subscription set) must carry the topic, and the broker's own
   ``_status_for_terminal_topic`` / ``_terminal_error_message`` must turn a
   record on it into ``failed`` plus an attributed cause.
3. :func:`test_real_dispatch_seam_routing_failure_reaches_the_boundary` — the
   real dispatch seam. ``wire_from_manifest`` with a real
   ``MessageDispatchEngine``, a real ``EventBusInmemory`` and the REAL
   ``HandlerRoutingIntent``, driving the verbatim ``ModelRoutingIntent`` wire
   record off the live topic, proving the raise reaches the boundary and is
   DLQ'd — and asserting the terminal itself on any omnibase-infra that
   carries the #2962 emitter.

SCOPE, STATED PLAINLY. The emission assertion in (3) is gated on the installed
omnibase-infra carrying ``runtime.boundary_failure_terminal``. #2962 is merged
to omnibase_infra ``dev`` and is NOT in a release; omnimarket pins
``omnibase-infra>=0.38.3,<0.39.0`` and resolves 0.38.11 from PyPI, which has
``_declared_failure_terminal_topics`` (OMN-15468) but no emitter. The ``.201``
dev lane builds omnibase_infra from the workspace, so the emission is live
there and gated only here. Claims (1) and (2) are unconditional and are what
makes the declaration provably sufficient rather than merely present.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.event_bus.topic_constants import get_dlq_topic_for_original
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _declared_failure_terminal_topics,
    _select_dispatch_result_output_topic,
    wire_from_manifest,
)
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.runtime_local_ingress import _extract_terminal_events
from omnibase_infra.runtime.service_pattern_b_broker import (
    _status_for_terminal_topic,
    _terminal_error_message,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REDUCER_DIR = (
    _REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_delegation_routing_reducer"
)
_REDUCER_CONTRACT = _REDUCER_DIR / "contract.yaml"

# Verbatim from the live incident trace (OMN-16812 ticket body / #2962 PR body).
_SUBSCRIBE_TOPIC = "onex.cmd.omnibase-infra.delegation-routing-request.v1"  # onex-topic-allow: verbatim from the live incident trace
_DECISION_TOPIC = "onex.evt.omnibase-infra.routing-decision.v1"  # onex-topic-allow: verbatim from the live incident trace
_FAILURE_TOPIC = "onex.evt.omnibase-infra.routing-decision-failed.v1"  # onex-topic-allow: the terminal this ticket declares
_LIVE_ONEX_CODE = "ONEX_CORE_041_INVALID_CONFIGURATION"

# #2962 shipped the emitter on omnibase_infra dev; it is not in a release yet.
_INFRA_HAS_BOUNDARY_EMITTER = (
    importlib.util.find_spec("omnibase_infra.runtime.boundary_failure_terminal")
    is not None
)
_EMITTER_GATE_REASON = (
    "installed omnibase-infra has no runtime.boundary_failure_terminal — "
    "omnibase_infra#2962 (OMN-16812) is merged to dev and unreleased, and "
    "omnimarket pins omnibase-infra>=0.38.3,<0.39.0 from PyPI. The declaration "
    "under test is what makes the emission fire wherever the emitter exists "
    "(the .201 dev lane builds omnibase_infra from the workspace)."
)

# A LOAD-ABLE bifrost contract whose only backends are `tier: local`, so a
# ``claude`` tier floor leaves no eligible tier and the REAL reducer raises the
# REAL ProtocolConfigurationError (ONEX_CORE_041_INVALID_CONFIGURATION) — the
# live .201 failure class, reached through the real config resolution rather
# than a stand-in raise. Schema copied from the fixture in
# ``tests/unit/delegation/test_handler_routing_intent.py``; a contract that
# fails to LOAD is not the same defect (it logs
# ``bifrost_endpoint_load_failed`` and takes a different path), so the schema
# has to be the real one.
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


def _raw_contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_REDUCER_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{_REDUCER_CONTRACT} is not a YAML mapping"
    return loaded


def _discovered_contract() -> Any:
    """The reducer as the RUNTIME discovers it — parsed from the real file."""
    manifest = discover_contracts_from_paths([_REDUCER_CONTRACT])
    assert not manifest.errors, f"contract discovery errored: {manifest.errors}"
    assert len(manifest.contracts) == 1
    return manifest.contracts[0]


# ---------------------------------------------------------------------------
# 1 — the emitter's own gate, evaluated on the real contract
# ---------------------------------------------------------------------------


def test_reducer_declares_exactly_one_failure_terminal() -> None:
    """``_emit_boundary_failure_terminal``'s first gate must resolve to ONE topic.

    RED before this ticket: ``_declared_failure_terminal_topics`` returned
    ``()`` for this contract, the emitter's ``len(...) != 1`` check returned
    early, and no terminal was ever published for a routing-handler raise.
    """
    contract = _discovered_contract()
    success_topic = _select_dispatch_result_output_topic(contract)
    assert success_topic == _DECISION_TOPIC

    failure_topics = _declared_failure_terminal_topics(
        contract, success_topic=success_topic
    )

    assert failure_topics == (_FAILURE_TOPIC,), (
        "the boundary emits only when the contract declares EXACTLY ONE failure "
        "terminal that is publishable and distinct from the success terminal; "
        f"got {failure_topics!r}"
    )
    # The emitter's second gate: a contract that both consumes and declares the
    # topic would republish its own failure onto its own subscription.
    assert failure_topics[0] != _SUBSCRIBE_TOPIC


def test_failure_terminal_is_declared_publishable_and_not_the_success_topic() -> None:
    """The declaration is only real if the contract may actually publish there.

    ``_declared_failure_terminal_topics`` intersects the declared terminals
    with ``event_bus.publish_topics`` precisely so a terminal declaration
    cannot authorize a publish the contract's own allowlist forbids.
    """
    raw = _raw_contract()
    publish_topics = raw["event_bus"]["publish_topics"]
    assert _FAILURE_TOPIC in publish_topics
    assert _DECISION_TOPIC in publish_topics

    declared = _extract_terminal_events(raw)
    assert declared[0] == _DECISION_TOPIC, (
        "success-first ordering is load-bearing: the Pattern B broker treats "
        "terminal_events[0] as the success topic when there is no top-level "
        "terminal_event"
    )
    assert _FAILURE_TOPIC in declared


# ---------------------------------------------------------------------------
# 2 — the terminal has an addressee, and reads as a typed failure there
# ---------------------------------------------------------------------------


def test_failure_terminal_is_awaited_by_the_local_ingress_route() -> None:
    """A terminal nobody awaits is a second silent drop.

    ``runtime_local_ingress._extract_terminal_events`` is what builds the
    Pattern B broker's subscription set for a dispatch aimed at this node, and
    it delegates to the same ``extract_terminal_event_topics`` reader the
    emitter's gate uses. Before this ticket it returned ``()`` for the reducer,
    so the broker awaited nothing and every reducer-targeted dispatch could
    only ever end in the ingress budget expiring.
    """
    terminals = _extract_terminal_events(_raw_contract())
    assert _FAILURE_TOPIC in terminals
    assert _DECISION_TOPIC in terminals


def test_broker_reads_a_boundary_terminal_as_an_attributed_failure() -> None:
    """AC2/AC3 at the reader: ``failed`` + originating class, not ``timeout``.

    The payload here is the WIRE shape — a decoded dict, what the broker
    actually holds — carrying the field names ``ModelBoundaryFailureTerminal``
    emits. Asserting against the decoded dict rather than the producer's model
    is deliberate: the broker never sees the model, and #2962's whole point is
    that the field names were chosen for readers that already exist.
    """
    raw = _raw_contract()
    terminals = _extract_terminal_events(raw)
    route = _FakeRoute(terminal_event=None, terminal_events=terminals)

    wire_payload: dict[str, object] = {
        "correlation_id": "7a300827-1000-4000-8000-000000000012",
        "status": "failed",
        "failure_class": "ProtocolConfigurationError",
        "failure_code": _LIVE_ONEX_CODE,
        "retryable": False,
        "failure_reason": (
            f"ProtocolConfigurationError: [{_LIVE_ONEX_CODE}] No tier has a "
            "configured endpoint for task_type='agent_delegation'"
        ),
        "origin_topic": _SUBSCRIBE_TOPIC,
    }

    status = _status_for_terminal_topic(route, _FAILURE_TOPIC, wire_payload)
    assert status == "failed"

    message = _terminal_error_message(wire_payload)
    assert message is not None
    assert "ProtocolConfigurationError" in message
    assert _LIVE_ONEX_CODE in message
    assert "timeout" not in message.casefold()

    # The success terminal still reads as completed — this is not "answer
    # failed to everything".
    assert _status_for_terminal_topic(route, _DECISION_TOPIC, None) == "completed"


class _FakeRoute:
    """Minimal stand-in for the two ``ModelRuntimeLocalIngressRoute`` fields read.

    ``_status_for_terminal_topic`` and ``_terminal_topics`` read exactly
    ``terminal_event`` and ``terminal_events``; both values here come from the
    REAL contract via ``_extract_terminal_events``.
    """

    def __init__(
        self, *, terminal_event: str | None, terminal_events: tuple[str, ...]
    ) -> None:
        self.terminal_event = terminal_event
        self.terminal_events = terminal_events


# ---------------------------------------------------------------------------
# 3 — the real dispatch seam
# ---------------------------------------------------------------------------


class _DlqRecordingInmemoryBus(EventBusInmemory):
    """In-memory bus honoring the boundary's duck-typed DLQ contract.

    ``EventBusInmemory`` has no ``_publish_raw_to_dlq``; ``EventBusKafka`` does,
    and that method is the only reason the boundary can preserve a record at
    all. Publishing it onto the topic's real DLQ address puts BOTH effects of
    one raise on the same observable bus. Mirrors the harness in
    omnibase_infra ``tests/unit/runtime/auto_wiring/
    test_omn16812_boundary_failure_terminal.py``.
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
                "consumer_group": consumer_group,
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


def _routing_intent_wire(correlation_id: UUID) -> dict[str, object]:
    """The ``ModelRoutingIntent`` as the orchestrator publishes it.

    ``min_tier_name="claude"`` is the deterministic trigger: with the local-only
    bifrost contract below, no tier at or above that floor has an endpoint, so
    the REAL ``delta()`` raises the REAL ``ProtocolConfigurationError`` with
    ONEX_CORE_041 — the live ``.201`` failure, reached through the real code.
    """
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


async def _drive_one_failing_record(
    *, collect_topics: tuple[str, ...]
) -> dict[str, list[ModelEventEnvelope[object]]]:
    correlation_id = uuid4()
    bus = _DlqRecordingInmemoryBus()
    await bus.start()
    seen: dict[str, list[ModelEventEnvelope[object]]] = {t: [] for t in collect_topics}
    arrived = asyncio.Event()

    def _collector(topic: str) -> Any:
        async def collect(message: ModelEventMessage) -> None:
            envelope = ModelEventEnvelope[object].model_validate_json(message.value)
            if envelope.correlation_id == correlation_id:
                seen[topic].append(envelope)
                arrived.set()

        return collect

    for topic in collect_topics:
        await bus.subscribe(
            topic, group_id=f"omn16812-{topic}", on_message=_collector(topic)
        )

    engine = MessageDispatchEngine()
    await wire_from_manifest(
        ModelAutoWiringManifest(contracts=(_discovered_contract(),)),
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
        _SUBSCRIBE_TOPIC, None, command.model_dump_json().encode("utf-8"), None
    )
    # A timeout here is not a test error: the assertions below report what
    # did or did not arrive, which is the finding either way.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(arrived.wait(), timeout=30)
    for _ in range(40):
        if all(seen[t] for t in collect_topics):
            break
        await asyncio.sleep(0.05)
    return seen


@pytest.fixture
def _local_only_bifrost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as routing

    original_config = routing._config
    routing._config = None
    routing._load_bifrost_endpoints.cache_clear()
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_LOCAL_ONLY_BIFROST, encoding="utf-8")
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    # OMN-14507's boundary DLQ is default-OFF behind this flag. The .201 dev
    # lane runs with it ON (the reproduction's `dlq_enabled=True` line), and
    # the terminal-emission path under test is reached from the DLQ-persisted
    # branch, so the lane's setting is the one to reproduce.
    monkeypatch.setenv("ONEX_BOUNDARY_DLQ_ENABLED", "true")
    yield
    routing._config = original_config
    routing._load_bifrost_endpoints.cache_clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_local_only_bifrost")
async def test_real_dispatch_seam_routing_failure_reaches_the_boundary() -> None:
    """AC4 — the raise travels the REAL wiring and is DLQ'd, not swallowed.

    An isolation test that calls ``HandlerRoutingIntent.handle`` directly passes
    through this entire outage: the defect was never in the handler. This drives
    ``wire_from_manifest`` with a real engine and a real bus against the real
    contract, so the wiring decisions under test are the ones production makes.

    This half is UNCONDITIONAL and is the pre-existing OMN-16798 behavior: the
    record is safe. It asserts what the caller does NOT get — no routing
    decision — which is the whole reason the caller was left waiting.
    """
    dlq_topic = get_dlq_topic_for_original(_SUBSCRIBE_TOPIC)
    seen = await _drive_one_failing_record(
        collect_topics=(dlq_topic, _DECISION_TOPIC, _FAILURE_TOPIC)
    )

    assert seen[dlq_topic], (
        "the routing raise never reached the consume boundary — the OMN-16798 "
        "DLQ guard should have parked the record"
    )
    dlq_payload = seen[dlq_topic][0].payload
    assert isinstance(dlq_payload, dict)
    # The live shape: the engine's catch-all already flattened the handler's
    # real exception, so the boundary holds a HandlerDispatchFailureError whose
    # MESSAGE is the only surviving trace of the cause. That is exactly why
    # `classify_boundary_failure` reads the flattened text, and it is what it
    # will read here.
    assert dlq_payload.get("error_type") == "HandlerDispatchFailureError"
    assert "ProtocolConfigurationError" in str(dlq_payload.get("error", "")), (
        "the boundary parked a record whose recorded error does not name the "
        f"routing configuration failure: {dlq_payload!r}"
    )
    assert not seen[_DECISION_TOPIC], "a failed routing must not emit a decision"


@pytest.mark.asyncio
@pytest.mark.skipif(not _INFRA_HAS_BOUNDARY_EMITTER, reason=_EMITTER_GATE_REASON)
@pytest.mark.usefixtures("_local_only_bifrost")
async def test_real_dispatch_seam_emits_the_declared_failure_terminal() -> None:
    """AC1/AC2/AC3 — one raise, two effects: the DLQ record AND the terminal.

    Gated on the installed omnibase-infra carrying #2962's emitter (see the
    module docstring). Everything the emitter reads to decide whether to fire
    is asserted unconditionally by
    :func:`test_reducer_declares_exactly_one_failure_terminal`, so a skip here
    means "the emitter is absent", never "the declaration is insufficient".
    """
    seen = await _drive_one_failing_record(
        collect_topics=(get_dlq_topic_for_original(_SUBSCRIBE_TOPIC), _FAILURE_TOPIC)
    )

    terminals = seen[_FAILURE_TOPIC]
    assert len(terminals) == 1, (
        "exactly one correlation-exact terminal on the contract's declared "
        f"failure terminal; got {len(terminals)} — an empty list is the 120 s "
        "dispatch_timeout the caller saw on the .201 dev lane"
    )
    payload = terminals[0].payload
    assert isinstance(payload, dict)
    assert payload["status"] == "failed"
    # AC2 — the ORIGINATING class, not the boundary wrapper
    # (HandlerDispatchFailureError), and the real ONEX code recovered from the
    # engine-flattened message.
    assert payload["failure_class"] == "ProtocolConfigurationError"
    assert payload["failure_code"] == _LIVE_ONEX_CODE
    # AC3 — a missing routing endpoint is not fixed by trying again.
    assert payload["retryable"] is False
    assert isinstance(payload["failure_reason"], str)
    assert payload["origin_topic"] == _SUBSCRIBE_TOPIC
