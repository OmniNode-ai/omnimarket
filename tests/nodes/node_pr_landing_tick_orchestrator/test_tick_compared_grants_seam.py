# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between this repository and the dev broker grants (OMN-23539).

The grants omnibase_infra declares let the developer principal produce on exactly
the tick orchestrator's compared and compare-failed topics. This module spells
that granted list on its side of the seam and fails when either side changes
shape: the contract must publish on exactly the granted topics, and a golden run
must send its compared events only there. The error chain publishes on a topic
outside the grants, proving the golden check can fail.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.handlers.overlay_store import bus_opener
from omnimarket.models.landing_controller_carry import (
    ModelLandingControllerDecisions,
    ModelLandingControllerLogged,
)
from omnimarket.models.landing_tick_record import ModelLandingTickRecord
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_bus_publisher import (
    BusTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    NodePathDecider,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickSnapshot,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    ModelTickOrchestratorOverlay,
    ModelTickTopicSelection,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.runtime_tick import run_once
from omnimarket.topic_namespace import apply_topic_namespace

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

# The granted list the seam holds on this side; omnibase_infra holds the other.
GRANTED_PRODUCE_TOPICS = (
    "onex.evt.omnimarket.pr-landing-tick-compared.v1",
    "onex.evt.omnimarket.pr-landing-tick-compare-failed.v1",
    # OMN-23830: the rerun request's answers; the grant rows are declared in the overlays repository
    # (broker_acls_mac_landing_tick) and the orchestrator applies them.
    "onex.evt.omnimarket.pr-landing-rerun-completed.v1",
    "onex.evt.omnimarket.pr-landing-rerun-failed.v1",
)


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


def _controller(snap: ModelTickSnapshot) -> ModelLandingControllerDecisions:
    """The old controller's finished tick as the record carries it: its decision rows, here the node path's own."""
    return ModelLandingControllerDecisions(
        controller_tick=1,
        observed_at=snap.facts.observed_at,
        decisions=tuple(
            ModelLandingControllerLogged(
                kind=action.kind, subject=action.subject, outcome="performed"
            )
            for action in NodePathDecider().decide(snap.facts, snap.fair_share).actions
        ),
        source="test",
    )


def _record(snap: ModelTickSnapshot) -> str:
    return ModelLandingTickRecord(
        correlation_id=uuid4(),
        tick=snap.facts.tick,
        facts=snap.facts,
        provenance="test",
        controller=_controller(snap),
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
        "host_env": "ONEX_HOST_NAME",
        "join_timeout_s": 5.0,
        "run_limit_s": 60,
        "run_margin_s": 5.0,
        "cutover_repository": repository,
        "script_out_of_scope_repositories": [repository] if repository else [],
    }
    values.update(update)
    return ModelTickOrchestratorOverlay.model_validate(values)


class _Recorder:
    """The bus the patched opener yields; publish lands in a list."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, bytes]] = []

    async def publish(self, topic: str, key: bytes, value: bytes) -> None:
        self.published.append((topic, key, value))


def _recording_bus(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Replace open_lane_bus with an opener that yields one shared recorder."""
    recorder = _Recorder()

    @asynccontextmanager
    async def _open_lane_bus(
        *,
        bus: str,
        lane: str | None,
        kafka_bootstrap: str | None,
        root: Path | None,
    ) -> AsyncIterator[_Recorder]:
        yield recorder

    monkeypatch.setattr(bus_opener, "open_lane_bus", _open_lane_bus)
    return recorder


def test_contract_publish_topics_are_exactly_the_granted_topics() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert set(contract["event_bus"]["publish_topics"]) == set(GRANTED_PRODUCE_TOPICS)


def test_bus_publisher_sends_compared_events_only_on_granted_topics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _recording_bus(monkeypatch)
    snap, repository = _cutover()
    code, line = run_once(
        _overlay(tmp_path, repository, publish_backend="redpanda"),
        topic_reader_factory=lambda _selection: _Reader([_record(snap)]),
    )
    assert code == 0, line
    assert recorder.published, "the compared event went to no bus"
    granted = {apply_topic_namespace(topic) for topic in GRANTED_PRODUCE_TOPICS}
    topics = [topic for topic, _key, _value in recorder.published]
    assert all(topic in granted for topic in topics)


class _ModelTickEvent(BaseModel):
    """Carries the tick the bus key reads (_key in tick_bus_publisher)."""

    tick: str


def test_a_topic_outside_the_grants_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _recording_bus(monkeypatch)
    publisher = BusTickPublisher(
        bus_lane="dev", workspace_root=str(tmp_path), bootstrap_servers=None
    )
    publisher.publish(
        "onex.evt.omnimarket.pr-landing-tick-not-granted.v1",
        _ModelTickEvent(tick="seam-control"),
    )
    granted = {apply_topic_namespace(topic) for topic in GRANTED_PRODUCE_TOPICS}
    topics = [topic for topic, _key, _value in recorder.published]
    assert topics, "the control published through no bus"
    assert all(topic not in granted for topic in topics)
