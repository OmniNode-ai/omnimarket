# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded dormant review, multipart sampling, and the runtime dispatch boundary."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring.discovery import _parse_contract
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    load_published_events_map,
)
from omnibase_infra.runtime.service_dispatch_result_applier import (
    build_contract_result_applier,
)

from omnimarket.events.topic_activity import (
    ModelTopicActivitySample,
    ModelTopicActivitySampleEvent,
)
from omnimarket.models.model_command_invocation import ModelCommandInvocation
from omnimarket.models.model_command_seam import (
    ModelCommandSeamFacts,
    ModelCommandSeamNode,
    ModelCommandSeamOverlay,
)
from omnimarket.nodes.node_seam_read_orchestrator.handlers import command_seam_inventory
from omnimarket.nodes.node_seam_read_orchestrator.handlers.handler_command_seam_census import (
    HandlerCommandSeamCensus,
)
from omnimarket.nodes.node_seam_reading_compute.handlers.handler_command_seam_reading import (
    HandlerCommandSeamReading,
)

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
REVIEW = "onex.cmd.omnimarket.agent-review-requested.v1"
ROOT = "onex.evt.platform.node-heartbeat.v1"
NODES = Path("src/omnimarket/nodes")


def _overlay() -> ModelCommandSeamOverlay:
    return ModelCommandSeamOverlay(
        scope="fixture/dev",
        manifest_urls=("https://runtime.invalid/manifest",),
        projection_url="https://runtime.invalid/projection",
        max_projection_pages=32,
        package_roots={"omnimarket": "src/omnimarket"},
        root_topics=(ROOT,),
        max_age_seconds=90,
        manifest_refresh_seconds=60,
        request_timeout_seconds=5,
        max_facts_per_event=100,
    )


def _review() -> ModelCommandSeamNode:
    return ModelCommandSeamNode(
        node="node_agent_review_dispatch_orchestrator",
        package="omnimarket",
        subscribes=(REVIEW,),
        command_topic=REVIEW,
        invocation=ModelCommandInvocation(
            mode="on_demand",
            sender="operator via agent review skill",
            reason="Recorded census found no automatic sender.",
            automatic_required=True,
        ),
    )


def _event(**changes: object) -> ModelTopicActivitySampleEvent:
    data: dict[str, object] = {
        "sample_id": "recorded-review",
        "sampled_at": NOW,
        "sample_interval_seconds": 30,
        "part_index": 0,
        "part_count": 1,
        "total_topic_count": 1,
        "empty_topic_count": 1,
        "broker_topics": (REVIEW,),
        "topics": (
            ModelTopicActivitySample(
                topic=REVIEW,
                high_watermark_total=0,
                low_watermark_total=0,
                messages_last_hour=0,
                messages_last_24h=0,
                retention_truncated=False,
            ),
        ),
    }
    return ModelTopicActivitySampleEvent.model_validate({**data, **changes})


async def _inventory() -> tuple[ModelCommandSeamNode, ...]:
    return (_review(),)


def test_recorded_review_is_not_working_even_with_manual_interface() -> None:
    census = HandlerCommandSeamCensus(
        overlay=_overlay(), inventory=_inventory, clock=lambda: NOW
    )
    output = asyncio.run(census.handle(_event()))
    (facts,) = output.events
    assert isinstance(facts, ModelCommandSeamFacts)
    reading = HandlerCommandSeamReading().handle(facts)
    (row,) = reading.readings
    assert row.state == "NOT_WORKING"
    assert row.reason == "required-automatic-trigger-unbound"
    assert row.last_crossed_at is None
    assert row.high_watermark == 0
    assert row.execution_evidence == "unmeasured"
    assert row.seven_day_absence_evidence == "unmeasured"
    assert reading.valid_until == NOW + timedelta(seconds=90)


def test_a_contract_publisher_without_a_root_is_not_automatic() -> None:
    async def inventory() -> tuple[ModelCommandSeamNode, ...]:
        return (
            _review(),
            ModelCommandSeamNode(
                node="untriggered_publisher",
                package="omnimarket",
                publishes=(REVIEW,),
            ),
        )

    census = HandlerCommandSeamCensus(
        overlay=_overlay(), inventory=inventory, clock=lambda: NOW
    )
    facts = asyncio.run(census.handle(_event())).events[0]
    assert isinstance(facts, ModelCommandSeamFacts)
    assert facts.facts[0].contract_senders == ("omnimarket/untriggered_publisher",)
    assert facts.facts[0].automatic_senders == ()


def test_clock_root_and_recent_publication_positive_control() -> None:
    async def inventory() -> tuple[ModelCommandSeamNode, ...]:
        return (
            _review(),
            ModelCommandSeamNode(
                node="review_scheduler",
                package="omnimarket",
                subscribes=(ROOT,),
                publishes=(REVIEW,),
            ),
        )

    sample = (
        _event()
        .topics[0]
        .model_copy(
            update={
                "high_watermark_total": 1,
                "newest_message_at": NOW - timedelta(seconds=5),
            }
        )
    )
    census = HandlerCommandSeamCensus(
        overlay=_overlay(), inventory=inventory, clock=lambda: NOW
    )
    facts = asyncio.run(
        census.handle(_event(topics=(sample,), empty_topic_count=0))
    ).events[0]
    assert isinstance(facts, ModelCommandSeamFacts)
    row = HandlerCommandSeamReading().handle(facts).readings[0]
    assert row.state == "WORKING"
    assert row.automatic_senders == ("omnimarket/review_scheduler",)
    assert row.last_crossed_at == NOW - timedelta(seconds=5)
    assert row.execution_evidence == "unmeasured"


def test_partial_sample_waits_for_all_parts_and_duplicate_does_not_republish() -> None:
    census = HandlerCommandSeamCensus(
        overlay=_overlay(), inventory=_inventory, clock=lambda: NOW
    )
    first = _event(part_count=2)
    second = _event(part_count=2, part_index=1, topics=())
    assert asyncio.run(census.handle(second)).events == ()
    assert len(asyncio.run(census.handle(first)).events) == 1
    assert asyncio.run(census.handle(first)).events == ()


def test_stale_sample_is_unknown_and_never_manufactures_absence() -> None:
    census = HandlerCommandSeamCensus(
        overlay=_overlay(),
        inventory=_inventory,
        clock=lambda: NOW + timedelta(minutes=5),
    )
    facts = asyncio.run(census.handle(_event())).events[0]
    assert isinstance(facts, ModelCommandSeamFacts)
    row = HandlerCommandSeamReading().handle(facts).readings[0]
    assert row.state == "UNKNOWN"
    assert row.reason == "stale-or-future-sample"


def test_manifest_read_failure_publishes_unknown_source_error() -> None:
    async def broken() -> tuple[ModelCommandSeamNode, ...]:
        raise TimeoutError("endpoint details must not enter the report")

    census = HandlerCommandSeamCensus(
        overlay=_overlay(), inventory=broken, clock=lambda: NOW
    )
    facts = asyncio.run(census.handle(_event())).events[0]
    assert isinstance(facts, ModelCommandSeamFacts)
    assert facts.errors == ("TimeoutError",)
    reading = HandlerCommandSeamReading().handle(facts)
    assert reading.errors == ("TimeoutError",)
    assert reading.readings == ()


def test_retained_empty_topic_keeps_projection_last_known_crossing() -> None:
    async def history() -> dict[str, datetime]:
        return {REVIEW: NOW - timedelta(days=2)}

    census = HandlerCommandSeamCensus(
        overlay=_overlay(),
        inventory=_inventory,
        last_crossings=history,
        clock=lambda: NOW,
    )
    facts = asyncio.run(census.handle(_event())).events[0]
    assert isinstance(facts, ModelCommandSeamFacts)
    row = HandlerCommandSeamReading().handle(facts).readings[0]
    assert row.last_crossed_at == NOW - timedelta(days=2)
    assert row.last_crossed_source == "projection_last_known"
    assert row.seven_day_absence_evidence == "unmeasured"


def test_inventory_refuses_to_attribute_an_uninstalled_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nodes/node_review/contract.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "runtime_dispatch": {
                    "command_topic": REVIEW,
                    "invocation": {
                        "mode": "on_demand",
                        "sender": "operator",
                        "reason": "diagnosis",
                    },
                }
            }
        )
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "errors": [],
        "contracts": [
            {
                "name": "review",
                "package_name": "omnimarket",
                "contract_path": "/installed/nodes/node_review/contract.yaml",
                "contract_content_hash": digest,
                "event_bus": {"subscribe_topics": [REVIEW]},
            }
        ],
    }
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=manifest))
    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        command_seam_inventory.httpx,
        "AsyncClient",
        lambda **kw: client_class(transport=transport, **kw),
    )
    overlay = _overlay().model_copy(
        update={"package_roots": {"omnimarket": str(tmp_path)}}
    )
    (node,) = asyncio.run(command_seam_inventory.load_runtime_inventory(overlay))
    assert node.invocation is not None
    assert node.binding_error is None
    path.write_text(path.read_text() + "# not installed\n")
    (node,) = asyncio.run(command_seam_inventory.load_runtime_inventory(overlay))
    assert node.invocation is None
    assert node.binding_error == "installed-contract-hash-mismatch"


def test_projection_history_requires_a_complete_cursor_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str | None] = []

    def respond(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("since")
        requests.append(cursor)
        return httpx.Response(
            200,
            json=(
                {"rows": [], "truncated": True, "next_cursor": 7}
                if cursor is None
                else {
                    "rows": [{"topic": REVIEW, "newest_message_at": NOW.isoformat()}],
                    "truncated": False,
                }
            ),
        )

    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        command_seam_inventory.httpx,
        "AsyncClient",
        lambda **kw: client_class(transport=transport, **kw),
    )
    assert asyncio.run(
        command_seam_inventory.load_topic_last_crossings(_overlay())
    ) == {REVIEW: NOW}
    assert requests == [None, "7"]
    with pytest.raises(ValueError, match="page-bound-exceeded"):
        asyncio.run(
            command_seam_inventory.load_topic_last_crossings(
                _overlay().model_copy(update={"max_projection_pages": 1})
            )
        )


def test_a_failed_refresh_marks_each_previously_known_seam_unknown() -> None:
    calls = 0
    now = NOW

    async def inventory() -> tuple[ModelCommandSeamNode, ...]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise TimeoutError("unreadable source")
        return (_review(),)

    census = HandlerCommandSeamCensus(
        overlay=_overlay(), inventory=inventory, clock=lambda: now
    )
    asyncio.run(census.handle(_event()))
    now = NOW + timedelta(seconds=61)
    facts = asyncio.run(census.handle(_event(sample_id="next", sampled_at=now))).events[
        0
    ]
    assert isinstance(facts, ModelCommandSeamFacts)
    assert facts.inventory_count == 1
    (row,) = HandlerCommandSeamReading().handle(facts).readings
    assert row.state == "UNKNOWN"
    assert row.reason == "source-unreadable"


def test_an_external_sender_without_an_invocation_mode_is_unknown() -> None:
    async def inventory() -> tuple[ModelCommandSeamNode, ...]:
        return (
            ModelCommandSeamNode(
                node="webhook",
                package="omnimarket",
                subscribes=(REVIEW,),
                external_senders={REVIEW: "declared HTTP webhook mount"},
            ),
        )

    census = HandlerCommandSeamCensus(
        overlay=_overlay(), inventory=inventory, clock=lambda: NOW
    )
    facts = asyncio.run(census.handle(_event())).events[0]
    assert isinstance(facts, ModelCommandSeamFacts)
    (row,) = HandlerCommandSeamReading().handle(facts).readings
    assert row.state == "UNKNOWN"
    assert row.reason == "external-sender-mode-unmeasured"
    assert row.declared_sender == "declared HTTP webhook mount"


def test_recent_projection_history_does_not_prove_the_current_log_is_working() -> None:
    async def inventory() -> tuple[ModelCommandSeamNode, ...]:
        return (
            _review(),
            ModelCommandSeamNode(
                node="review_scheduler",
                package="omnimarket",
                subscribes=(ROOT,),
                publishes=(REVIEW,),
            ),
        )

    async def history() -> dict[str, datetime]:
        return {REVIEW: NOW - timedelta(seconds=5)}

    census = HandlerCommandSeamCensus(
        overlay=_overlay(),
        inventory=inventory,
        last_crossings=history,
        clock=lambda: NOW,
    )
    facts = asyncio.run(census.handle(_event())).events[0]
    assert isinstance(facts, ModelCommandSeamFacts)
    (row,) = HandlerCommandSeamReading().handle(facts).readings
    assert row.state == "UNKNOWN"
    assert row.reason == "historical-crossing-current-log-unmeasured"
    assert row.last_crossed_at == NOW - timedelta(seconds=5)


def test_typed_runtime_adapter_and_publication_map_carry_both_hops() -> None:
    """Exercise the same contract discovery and payload adapter as runtime wiring."""
    census_path = NODES / "node_seam_read_orchestrator/contract.yaml"
    reading_path = NODES / "node_seam_reading_compute/contract.yaml"
    census_contract = _parse_contract(
        contract_path=census_path,
        entry_point_name="node_seam_read_orchestrator",
        package_name="omnimarket",
        package_version="0.0.0",
    )
    reading_contract = _parse_contract(
        contract_path=reading_path,
        entry_point_name="node_seam_reading_compute",
        package_name="omnimarket",
        package_version="0.0.0",
    )
    assert census_contract is not None
    assert reading_contract is not None
    assert census_contract.handler_routing is not None
    assert reading_contract.handler_routing is not None
    handler = HandlerCommandSeamCensus(
        overlay=_overlay(), inventory=_inventory, clock=lambda: NOW
    )
    route = census_contract.handler_routing.handlers[0]
    callback = _make_dispatch_callback(handler, route.event_model)
    dispatched = asyncio.run(
        callback(ModelEventEnvelope(payload=_event().model_dump(mode="json")))
    )
    assert dispatched is not None
    (facts,) = dispatched.output_events
    assert isinstance(facts, ModelCommandSeamFacts)

    class Bus:
        def __init__(self) -> None:
            self.sent: list[tuple[str, ModelEventEnvelope]] = []

        async def publish_envelope(
            self, *, envelope: ModelEventEnvelope, topic: str, key: bytes | None = None
        ) -> None:
            del key
            self.sent.append((topic, envelope))

    bus = Bus()
    applier = build_contract_result_applier(
        event_bus=bus,
        contract_path=census_path,
        publish_topics=census_contract.event_bus.publish_topics,
        terminal_event=census_contract.terminal_event,
    )
    asyncio.run(applier.apply(dispatched))
    assert (
        bus.sent[0][0] == load_published_events_map(census_path)[type(facts).__name__]
    )
    compute_route = next(
        r
        for r in reading_contract.handler_routing.handlers
        if r.operation == "command_seam_reading"
    )
    callback = _make_dispatch_callback(
        HandlerCommandSeamReading(), compute_route.event_model
    )
    computed = asyncio.run(callback(bus.sent[0][1]))
    assert computed is not None
    (reading,) = computed.output_events
    assert reading.readings[0].state == "NOT_WORKING"
    applier = build_contract_result_applier(
        event_bus=bus,
        contract_path=reading_path,
        publish_topics=reading_contract.event_bus.publish_topics,
        terminal_event=reading_contract.terminal_event,
    )
    asyncio.run(applier.apply(computed))
    assert (
        bus.sent[1][0]
        == load_published_events_map(reading_path)[type(reading).__name__]
    )
    assert (
        load_published_events_map(census_path)[type(facts).__name__]
        in census_contract.event_bus.publish_topics
    )
    assert (
        load_published_events_map(reading_path)[type(reading).__name__]
        in reading_contract.event_bus.publish_topics
    )
