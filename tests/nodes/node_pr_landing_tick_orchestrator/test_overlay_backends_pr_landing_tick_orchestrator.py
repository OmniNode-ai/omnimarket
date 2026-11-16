# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay selects the backend: in-memory by default, Redpanda behind a topic reader (OMN-22921, OMN-23458)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.models.landing_tick_record import ModelLandingTickRecord
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
    InMemoryTickWriter,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_sources import (
    InMemorySnapshotSource,
    RedpandaSnapshotSource,
    build_snapshot_source,
    load_tick_overlay,
    run_snapshots,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_topic_reader import (
    BusTickTopicReader,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    EnumTickStatus,
    ModelTickSnapshot,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    OVERLAY_ID,
    ModelTickOrchestratorOverlay,
    ModelTickTopicSelection,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "pr_landing_tick_replay"
PATHS = sorted(FIXTURES.glob("*.json"))[:4]
RAW = [p.read_text(encoding="utf-8") for p in PATHS]
# What the landing snapshot step publishes on its record_topic: the shared tick record (OMN-23409).
RECORDS = [
    ModelLandingTickRecord(
        correlation_id=UUID(int=i + 1),
        tick=ModelTickSnapshot.model_validate_json(r).facts.tick,
        facts=ModelTickSnapshot.model_validate_json(r).facts,
        provenance="snapshot-step",
    ).model_dump_json()
    for i, r in enumerate(RAW)
]
INMEMORY = {
    "backend": "inmemory",
    "act_enabled": False,
    "max_snapshots": 10,
    "floor_piece": 9,
}
REDPANDA = {
    **INMEMORY,
    "backend": "redpanda",
    "bootstrap_servers": "broker.example:9092",
    "consumer_group": "landing-tick-observe",
    "snapshot_topic": "onex.evt.example.landing-tick-record.v1",
    "poll_timeout_s": 2.0,
    "host_env": "ONEX_HOST_NAME",
    "join_timeout_s": 5.0,
    "run_limit_s": 60,
    "run_margin_s": 5.0,
    "emission_journal": ".onex_state/example/emissions.jsonl",
}
REDPANDA_LANE = {
    **{k: v for k, v in REDPANDA.items() if k != "bootstrap_servers"},
    "bus_lane": "dev",
    "workspace_root": "/ws",
}


class _Reader:
    def __init__(self, calls: list[tuple[int, float]]) -> None:
        self._calls = calls

    def read(self, max_records: int, timeout_s: float) -> Iterable[str]:
        self._calls.append((max_records, timeout_s))
        return RECORDS


def test_inmemory_overlay_runs_every_snapshot_in_observe_mode_with_no_write() -> None:
    overlay = load_tick_overlay(explicit=INMEMORY)
    source = build_snapshot_source(
        overlay,
        inmemory=InMemorySnapshotSource(
            ModelTickSnapshot.model_validate_json(r) for r in RAW
        ),
    )
    writer, publisher = InMemoryTickWriter(), InMemoryTickPublisher()
    handler = HandlerPrLandingTickOrchestrator(
        writer=writer, publisher=publisher, act_enabled=overlay.act_enabled
    )
    results = run_snapshots(handler, source, EnumTickMode.OBSERVE)
    assert len(results) == len(RAW)
    assert all(r.status is EnumTickStatus.COMPARED and r.equal for r in results)
    assert writer.performed == []
    assert len(publisher.published) == len(RAW)


def test_redpanda_overlay_reads_the_snapshot_step_tick_records_through_the_injected_reader() -> (
    None
):
    calls: list[tuple[int, float]] = []
    overlay = load_tick_overlay(explicit=REDPANDA)
    seen: list[ModelTickTopicSelection] = []

    def factory(selection: ModelTickTopicSelection) -> _Reader:
        seen.append(selection)
        return _Reader(calls)

    source = build_snapshot_source(overlay, topic_reader_factory=factory)
    assert isinstance(source, RedpandaSnapshotSource)
    assert seen == [
        ModelTickTopicSelection(
            topic="onex.evt.example.landing-tick-record.v1",
            consumer_group="landing-tick-observe-test-host",
            join_timeout_s=5.0,
            bootstrap_servers="broker.example:9092",
        )
    ]
    snapshots = source.snapshots()
    assert len(snapshots) == len(RAW)
    assert all(
        s.decision is None and s.provenance == "snapshot-step" for s in snapshots
    )
    assert calls == [(10, 2.0)]


def test_a_redpanda_backend_without_a_reader_factory_reads_through_the_bus_reader() -> (
    None
):
    source = build_snapshot_source(load_tick_overlay(explicit=REDPANDA))
    assert isinstance(source, RedpandaSnapshotSource)
    assert isinstance(source._reader, BusTickTopicReader)


def test_a_lane_overlay_selects_the_lane_and_no_broker_address() -> None:
    overlay = load_tick_overlay(explicit=REDPANDA_LANE)
    seen: list[ModelTickTopicSelection] = []

    def factory(selection: ModelTickTopicSelection) -> _Reader:
        seen.append(selection)
        return _Reader([])

    build_snapshot_source(overlay, topic_reader_factory=factory)
    assert len(seen) == 1
    assert seen[0].bus_lane == "dev"
    assert seen[0].bootstrap_servers is None
    assert seen[0].workspace_root == "/ws"


def test_a_redpanda_overlay_naming_neither_lane_nor_broker_is_refused() -> None:
    with pytest.raises(ValueError, match="bus_lane or bootstrap_servers"):
        load_tick_overlay(
            explicit={k: v for k, v in REDPANDA.items() if k != "bootstrap_servers"}
        )


def test_an_overlay_naming_both_lane_and_broker_is_refused() -> None:
    # the overlay loader names the document only; the model names the conflict
    with pytest.raises(ValueError, match="one of bus_lane or bootstrap_servers"):
        ModelTickOrchestratorOverlay.model_validate(
            {**REDPANDA, "bus_lane": "dev", "workspace_root": "/ws"}
        )


def test_a_lane_without_its_workspace_root_is_refused() -> None:
    with pytest.raises(ValueError, match="bus_lane needs workspace_root"):
        ModelTickOrchestratorOverlay.model_validate(
            {k: v for k, v in REDPANDA_LANE.items() if k != "workspace_root"}
        )


def test_a_redpanda_overlay_missing_its_fields_is_refused_naming_them() -> None:
    with pytest.raises(ValueError, match="snapshot_topic"):
        load_tick_overlay(
            explicit={k: v for k, v in REDPANDA.items() if k != "snapshot_topic"}
        )


def test_a_redpanda_overlay_without_the_host_identity_or_the_run_bounds_is_refused() -> (
    None
):
    with pytest.raises(ValueError, match="host_env"):
        ModelTickOrchestratorOverlay.model_validate({**INMEMORY, "backend": "redpanda"})


def test_no_overlay_is_refused_naming_the_overlay() -> None:
    with pytest.raises(ValueError, match=OVERLAY_ID):
        load_tick_overlay()


def test_an_unknown_overlay_field_is_refused() -> None:
    with pytest.raises(ValueError, match="mystery"):
        load_tick_overlay(explicit={**INMEMORY, "mystery": 1})
