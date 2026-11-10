# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay's publish_backend selects the tick orchestrator's publisher (OMN-23539).

The in-memory publisher stays the default; redpanda selects the bus publisher of
tick_bus_publisher, whose bus lane or lab broker the overlay names. A redpanda
publish_backend without a broker is refused. Golden: one compared record publishes through
the publisher the overlay selects, on a topic the contract declares.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.landing_tick_record import ModelLandingTickRecord
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers import (
    tick_bus_publisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_bus_publisher import (
    BusTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
    NodePathDecider,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_sources import (
    InMemorySnapshotSource,
    build_publisher,
    run_snapshots,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    ModelTickSnapshot,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    EnumTickBackend,
    ModelTickOrchestratorOverlay,
    ModelTickTopicSelection,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.runtime_tick import run_once

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "pr_landing_tick_replay"
    / "scenario_s22_green_disarmed_hold_release.json"
)
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_landing_tick_orchestrator"
    / "contract.yaml"
)
RECORD_TOPIC = "onex.evt.omnimarket.pr-landing-tick-facts-recorded.v1"


class _Reader:
    def __init__(self, raw: list[str]) -> None:
        self._raw = raw
        self.selection: ModelTickTopicSelection | None = None

    def read(self, max_records: int, timeout_s: float) -> Iterable[str]:
        return self._raw[:max_records]


def _cutover() -> tuple[ModelTickSnapshot, str]:
    snap = ModelTickSnapshot.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    decision = NodePathDecider().decide(snap.facts, snap.fair_share)
    repos = [a.subject.partition("#")[0] for a in decision.actions if "#" in a.subject]
    assert repos, "the fixture tick holds no pull request action"
    return snap, repos[0]


def _record(snap: ModelTickSnapshot) -> str:
    return ModelLandingTickRecord(
        correlation_id=uuid4(),
        tick=snap.facts.tick,
        facts=snap.facts,
        provenance="test",
    ).model_dump_json()


def _overlay(
    tmp_path: Path, repository: str | None, **update: object
) -> ModelTickOrchestratorOverlay:
    values: dict[str, object] = {
        "backend": "redpanda",
        "act_enabled": False,
        "max_snapshots": 10,
        "floor_piece": 9,
        "emission_journal": str(tmp_path / "emissions.jsonl"),
        "bus_lane": "dev",
        "workspace_root": str(tmp_path),
        "consumer_group": "landing-tick-cutover",
        "snapshot_topic": RECORD_TOPIC,
        "poll_timeout_s": 1.0,
        "cutover_repository": repository,
        "script_out_of_scope_repositories": [repository] if repository else [],
    }
    values.update(update)
    return ModelTickOrchestratorOverlay.model_validate(values)


def _without_correlation(
    published: list[tuple[str, object]],
) -> list[tuple[str, dict[str, object]]]:
    # A fresh run mints a new correlation id per record, so the events compare without it.
    return [
        (
            topic,
            {
                key: value
                for key, value in event.model_dump(mode="json").items()
                if key != "correlation_id"
            },
        )
        for topic, event in published
    ]


def test_publish_backend_defaults_to_inmemory(tmp_path: Path) -> None:
    overlay = _overlay(tmp_path, None)
    assert overlay.publish_backend is EnumTickBackend.INMEMORY
    assert isinstance(build_publisher(overlay), InMemoryTickPublisher)


def test_publish_backend_redpanda_builds_the_bus_publisher(tmp_path: Path) -> None:
    overlay = _overlay(tmp_path, None, publish_backend="redpanda")
    publisher = build_publisher(overlay)
    assert isinstance(publisher, BusTickPublisher)
    assert publisher._lane == "dev"
    assert publisher._root == str(tmp_path)


def test_publish_backend_redpanda_without_a_broker_is_refused() -> None:
    values: dict[str, object] = {
        "backend": "redpanda",
        "act_enabled": False,
        "max_snapshots": 10,
        "floor_piece": 9,
        "consumer_group": "landing-tick-cutover",
        "snapshot_topic": RECORD_TOPIC,
        "poll_timeout_s": 1.0,
        "publish_backend": "redpanda",
    }
    with pytest.raises(ValueError, match="publish_backend"):
        ModelTickOrchestratorOverlay.model_validate(values)


def test_run_once_publishes_compared_events_through_the_selected_publisher(
    tmp_path: Path,
) -> None:
    snap, repository = _cutover()
    reader = _Reader([_record(snap)])
    recording = InMemoryTickPublisher()
    code, line = run_once(
        _overlay(tmp_path, repository, publish_backend="redpanda"),
        topic_reader_factory=lambda _selection: reader,
        publisher_factory=lambda _overlay: recording,
    )
    assert code == 0, line
    assert line["status"] == "compared"
    expected = InMemoryTickPublisher()
    handler = HandlerPrLandingTickOrchestrator(
        publisher=expected,
        act_enabled=False,
        floor_piece=9,
        cutover_repository=repository,
        script_out_of_scope=(repository,),
    )
    run_snapshots(
        handler,
        InMemorySnapshotSource(
            [ModelTickSnapshot(facts=snap.facts, provenance="test")]
        ),
        EnumTickMode.OBSERVE,
    )
    assert _without_correlation(recording.published) == _without_correlation(
        expected.published
    )
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    publish_topics = [str(topic) for topic in contract["event_bus"]["publish_topics"]]
    topics = [topic for topic, _ in recording.published]
    assert topics, "the compared event went to no publisher"
    assert all(topic in publish_topics for topic in topics)


def test_run_once_without_a_publisher_factory_and_inmemory_publish_backend_never_opens_a_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refused(self: BusTickPublisher, **_kwargs: object) -> None:
        raise AssertionError(
            "the inmemory publish backend must not construct the bus publisher"
        )

    monkeypatch.setattr(tick_bus_publisher.BusTickPublisher, "__init__", _refused)
    snap, repository = _cutover()
    reader = _Reader([_record(snap)])
    code, line = run_once(
        _overlay(tmp_path, repository),
        topic_reader_factory=lambda _selection: reader,
    )
    assert code == 0, line
    assert line["status"] == "compared"
