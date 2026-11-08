# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay selects the backend: in-memory by default, Redpanda behind an injected reader (OMN-22921)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

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
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    EnumTickStatus,
    ModelTickSnapshot,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    OVERLAY_ID,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "pr_landing_tick_replay"
PATHS = sorted(FIXTURES.glob("*.json"))[:4]
RAW = [p.read_text(encoding="utf-8") for p in PATHS]
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
    "emission_journal": ".onex_state/example/emissions.jsonl",
}


class _Reader:
    def __init__(self, calls: list[tuple[int, float]]) -> None:
        self._calls = calls

    def read(self, max_records: int, timeout_s: float) -> Iterable[str]:
        self._calls.append((max_records, timeout_s))
        return RAW


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


def test_redpanda_overlay_reads_the_script_tick_records_through_the_injected_reader() -> (
    None
):
    calls: list[tuple[int, float]] = []
    overlay = load_tick_overlay(explicit=REDPANDA)
    seen: list[tuple[str, str, str]] = []

    def factory(servers: str, topic: str, group: str) -> _Reader:
        seen.append((servers, topic, group))
        return _Reader(calls)

    source = build_snapshot_source(overlay, topic_reader_factory=factory)
    assert isinstance(source, RedpandaSnapshotSource)
    assert seen == [
        (
            "broker.example:9092",
            "onex.evt.example.landing-tick-record.v1",
            "landing-tick-observe",
        )
    ]
    assert len(source.snapshots()) == len(RAW)
    assert calls == [(10, 2.0)]


def test_a_redpanda_overlay_missing_its_fields_is_refused_naming_them() -> None:
    with pytest.raises(ValueError, match="snapshot_topic"):
        load_tick_overlay(explicit={**INMEMORY, "backend": "redpanda"})


def test_no_overlay_is_refused_naming_the_overlay() -> None:
    with pytest.raises(ValueError, match=OVERLAY_ID):
        load_tick_overlay()


def test_an_unknown_overlay_field_is_refused() -> None:
    with pytest.raises(ValueError, match="mystery"):
        load_tick_overlay(explicit={**INMEMORY, "mystery": 1})


def test_a_redpanda_backend_without_a_reader_factory_is_refused() -> None:
    with pytest.raises(ValueError, match="topic reader factory"):
        build_snapshot_source(load_tick_overlay(explicit=REDPANDA))
