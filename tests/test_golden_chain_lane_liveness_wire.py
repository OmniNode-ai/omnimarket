# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: lane liveness is gathered on a schedule and a drop is alerted.

node_lane_liveness_compute subscribed to a command topic nothing published and
published an evaluation nothing read, so a dropped lane was found only when a
person ran scripts/lane_liveness_reader.py. These tests bind the gather node
(tick -> command) and the drop-alert node (evaluation -> Slack command) to the
compute node's declared topics, from the contracts on disk.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.liveness.model_lane_liveness import (
    EnumEvidenceBasis,
    EnumLaneVerdict,
    EnumRelayState,
    ModelLaneLivenessReport,
    ModelLaneLivenessRequest,
    ModelLaneVerdict,
)
from omnimarket.nodes.node_lane_liveness_drop_alert_effect.handlers.handler_lane_liveness_drop_alert import (
    HandlerLaneLivenessDropAlert,
)
from omnimarket.nodes.node_lane_liveness_gather_effect.handlers.handler_lane_liveness_gather import (
    HandlerLaneLivenessGather,
    ModelLaneWindowRead,
)
from omnimarket.validators.contract_topic_graph import GRAPH_PACKAGES, build_graph

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[1] / "src" / "omnimarket" / "nodes"
COMPUTE = "node_lane_liveness_compute"
GATHER = "node_lane_liveness_gather_effect"
ALERT = "node_lane_liveness_drop_alert_effect"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
NOW = dt.datetime(2026, 10, 5, 14, 0, tzinfo=dt.UTC)


def _contract(node: str) -> dict[str, Any]:
    return yaml.safe_load((NODES / node / "contract.yaml").read_text())


def _tick(now: dt.datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="test-scheduler",
        tick_interval_ms=1000,
    )


class _FakeReader:
    def __init__(self, read: ModelLaneWindowRead) -> None:
        self.read = read
        self.calls: list[tuple[dt.datetime, dt.datetime]] = []

    def read_window(
        self, window_start: dt.datetime, window_end: dt.datetime
    ) -> ModelLaneWindowRead:
        self.calls.append((window_start, window_end))
        return self.read


def _window_read() -> ModelLaneWindowRead:
    return ModelLaneWindowRead(
        lane_rows=({"lane": "lane-a", "last_event_at": NOW, "event_count": 4},),
        relay_last_event_at=NOW,
        relay_event_count=9,
        attributed_event_count=4,
        claims={"lane-a": NOW - dt.timedelta(minutes=30)},
        terminals={},
    )


def _report(verdict: EnumLaneVerdict) -> ModelLaneLivenessReport:
    return ModelLaneLivenessReport(
        window_start=NOW - dt.timedelta(hours=1),
        window_end=NOW,
        relay_state=EnumRelayState.CARRYING,
        relay_last_event_at=NOW,
        relay_event_count=9,
        relay_silent_seconds=0,
        lane_attribution_available=True,
        verdicts=(
            ModelLaneVerdict(
                lane="lane-a",
                verdict=verdict,
                evidence_basis=EnumEvidenceBasis.HOOK_EVENTS,
                reason="silent past threshold",
                silent_seconds=1200,
            ),
        ),
    )


def _graph(tmp_path: Path) -> Any:
    roots = {package: tmp_path / package for package in GRAPH_PACKAGES}
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    roots["omnimarket"] = NODES.parent
    return build_graph(roots=roots)


def test_compute_command_topic_has_a_node_producer(tmp_path: Path) -> None:
    topic = _contract(COMPUTE)["runtime_dispatch"]["command_topic"]
    producers = _graph(tmp_path).producers.get(topic, ())
    assert any(GATHER in p for p in producers), f"{topic} producers={producers}"


def test_compute_evaluation_topic_has_a_node_consumer(tmp_path: Path) -> None:
    topic = _contract(COMPUTE)["terminal_event"]
    consumers = _graph(tmp_path).consumers.get(topic, ())
    assert any(ALERT in c for c in consumers), f"{topic} consumers={consumers}"


def test_gather_is_scheduled_from_the_tick_and_publishes_the_command() -> None:
    bus = _contract(GATHER)["event_bus"]
    command = _contract(COMPUTE)["runtime_dispatch"]["command_topic"]
    assert bus["subscribe_topics"] == [TICK_TOPIC]
    assert bus["publish_topics"] == [command]


def test_compute_contract_unchanged_by_the_wire() -> None:
    contract = _contract(COMPUTE)
    assert contract["event_bus"] == {
        "subscribe_topics": ["onex.cmd.omnimarket.lane-liveness-requested.v1"],
        "publish_topics": ["onex.evt.omnimarket.lane-liveness-evaluated.v1"],
    }
    assert contract["input_model"].endswith(
        "models.model_lane_liveness.ModelLaneLivenessRequest"
    )


def test_tick_gathers_a_request_from_both_surfaces() -> None:
    reader = _FakeReader(_window_read())
    request = HandlerLaneLivenessGather(reader=reader).handle(_tick(NOW))
    assert isinstance(request, ModelLaneLivenessRequest)
    assert request.window_end == NOW
    assert request.lane_attribution_available is True
    assert [o.lane for o in request.observations] == ["lane-a"]
    assert request.observations[0].claimed_at is not None
    assert request.observations[0].hook_event_count == 4


def test_a_second_tick_inside_the_interval_gathers_nothing() -> None:
    reader = _FakeReader(_window_read())
    handler = HandlerLaneLivenessGather(reader=reader)
    assert handler.handle(_tick(NOW)) is not None
    assert handler.handle(_tick(NOW + dt.timedelta(seconds=30))) is None
    assert len(reader.calls) == 1


def test_a_failed_read_is_raised_not_reported_as_a_quiet_fleet() -> None:
    class _Broken:
        def read_window(self, *_: object) -> ModelLaneWindowRead:
            raise ConnectionError("db down")

    with pytest.raises(ConnectionError):
        HandlerLaneLivenessGather(reader=_Broken()).handle(_tick(NOW))


def test_a_dropped_lane_yields_a_slack_command_naming_it() -> None:
    command = HandlerLaneLivenessDropAlert(channel_resolver=lambda: "C0TEST").handle(
        _report(EnumLaneVerdict.DROPPED)
    )
    assert command is not None
    assert command.channel == "C0TEST"
    assert "lane-a" in command.text
    assert command.idempotency_key


def test_no_dropped_lane_yields_no_alert() -> None:
    handler = HandlerLaneLivenessDropAlert(channel_resolver=lambda: "C0TEST")
    for verdict in (
        EnumLaneVerdict.ALIVE,
        EnumLaneVerdict.TERMINATED,
        EnumLaneVerdict.UNOBSERVABLE,
        EnumLaneVerdict.UNKNOWN_RELAY_SILENT,
    ):
        assert handler.handle(_report(verdict)) is None


def test_the_same_drop_in_the_next_window_dedupes_on_the_key() -> None:
    handler = HandlerLaneLivenessDropAlert(channel_resolver=lambda: "C0TEST")
    first = handler.handle(_report(EnumLaneVerdict.DROPPED))
    later = _report(EnumLaneVerdict.DROPPED).model_copy(
        update={"window_end": NOW + dt.timedelta(minutes=5)}
    )
    second = handler.handle(later)
    assert first is not None
    assert second is not None
    assert first.idempotency_key == second.idempotency_key


def test_the_alert_publishes_to_the_slack_publish_command_topic() -> None:
    contract = _contract(ALERT)
    slack = _contract("node_slack_publish_effect")["runtime_dispatch"]["command_topic"]
    assert slack == "onex.cmd.omnimarket.slack-publish.v1"
    assert contract["terminal_event"] == slack
    assert contract["event_bus"]["publish_topics"] == [slack]
    assert contract["event_bus"]["subscribe_topics"] == [
        _contract(COMPUTE)["terminal_event"]
    ]
