# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scheduled round heartbeats reach the judgment, and its alert record reaches the alarm node once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path

import pytest
import yaml

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryOutcome,
    EnumDeliveryStep,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay
from omnimarket.nodes.node_work_progress_compute import NodeWorkEpisodesCompute
from omnimarket.nodes.node_work_progress_compute.models import (
    EnumEpisodeClosed,
    ModelWorkEpisodeRequest,
    ModelWorkEpisodeResult,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
START = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
LIMITS = {"round": {"awaiting_next_run": 7200}}


def _beat(minutes: int, tick: str, job: str = "floor_fill") -> ModelScheduleRunEvent:
    when = START + timedelta(minutes=minutes)
    return ModelScheduleRunEvent(
        job_id=job,
        node_name=f"node_{job}_scheduled_effect",
        tick_id=tick,
        scheduled_for=when,
        fired_at=when,
        outcome="completed",
    )


def _judge(
    beats: tuple[ModelScheduleRunEvent, ...],
    at: datetime,
    carried: ModelWorkEpisodeResult | None = None,
    job_budgets: dict[str, int] | None = None,
) -> ModelWorkEpisodeResult:
    return NodeWorkEpisodesCompute().handle(
        ModelWorkEpisodeRequest(
            correlation_id=f"corr-{at.isoformat()}",
            now=at,
            budgets=LIMITS,
            round_heartbeats=beats,
            round_job_budgets=job_budgets or {},
            open_episodes=carried.open_episodes if carried else (),
        )
    )


def _delivery() -> tuple[
    HandlerLabAlarmDeliver, InMemoryAlarmChannel, InMemoryAlarmLedger
]:
    overlay = ModelDeliveryOverlay.model_validate(
        yaml.safe_load(
            (_ROOT / "node_lab_alarm_deliver_effect/overlay.example.yaml").read_text(
                "utf-8"
            )
        )
    )
    channel, ledger = InMemoryAlarmChannel(), InMemoryAlarmLedger()
    handler = HandlerLabAlarmDeliver(
        overlay=overlay,
        channel=channel,
        ledger=ledger,
        state=InMemoryDeliveryState(),
        clock=lambda: START + timedelta(hours=5),
    )
    return handler, channel, ledger


def test_two_evaluations_of_one_stalled_round_emit_one_alarm_record() -> None:
    beats = (_beat(0, "tick-0"), _beat(60, "tick-1"))
    # Newest heartbeat at 09:00; the limit is 7200 s, so 11:00 is on the line and 11:05 is over it.
    assert _judge(beats, START + timedelta(hours=3)).alarm_request is None
    first = _judge(beats, START + timedelta(hours=3, minutes=5))
    assert first.alarm_request is not None
    assert [a.subject for a in first.alarm_request.alarms] == [
        "round|floor_fill|awaiting_next_run|tick-1"
    ]
    second = _judge(beats, START + timedelta(hours=3, minutes=10), carried=first)
    assert second.crossings == ()
    assert second.alarm_request is None

    # The records the node handed out, as the alarm node's input model, deliver one post and one row.
    records = [r.alarm_request for r in (first, second) if r.alarm_request]
    assert len(records) == 1
    handler, channel, ledger = _delivery()
    for record in records:
        assert isinstance(record, ModelLabAlarmDeliveryRequest)
        result = handler.handle(record)
        assert result.outcome is EnumDeliveryOutcome.DELIVERED
        assert result.deliveries[0].channel is EnumDeliveryStep.DELIVERED
    assert len(channel.posts) == 1
    assert len(ledger.rows) == 1
    # The alarm node's own edge trigger agrees: the same record again posts nothing.
    again = handler.handle(records[0])
    assert again.outcome is EnumDeliveryOutcome.NOTHING_NEW
    assert len(channel.posts) == 1


def test_a_newer_heartbeat_closes_the_round_episode_and_clears_the_alarm_node() -> None:
    beats = (_beat(0, "tick-0"),)
    at = START + timedelta(hours=2, minutes=1)
    first = _judge(beats, at)
    assert first.alarm_request is not None

    resumed = (*beats, _beat(130, "tick-1"))
    closed = _judge(resumed, at + timedelta(minutes=10), carried=first)
    assert [r.reason for r in closed.recoveries] == [EnumEpisodeClosed.MOVED]
    assert closed.open_episodes == ()
    assert closed.alarm_request is not None

    handler, channel, _ = _delivery()
    handler.handle(first.alarm_request)
    cleared = handler.handle(closed.alarm_request)
    assert cleared.cleared == (first.alarm_request.alarms[0].key,)
    assert len(channel.posts) == 1


def test_each_job_is_judged_on_its_own_newest_heartbeat_and_override() -> None:
    beats = (
        _beat(0, "a-0", "job_a"),
        _beat(110, "a-1", "job_a"),
        _beat(0, "b-0", "job_b"),
    )
    at = START + timedelta(hours=2, minutes=5)
    result = _judge(beats, at, job_budgets={"job_b": 600})
    # job_a's newest heartbeat is 15 minutes old; job_b's is two hours old against its own 10 minute limit.
    assert [c.key for c in result.crossings] == ["job_b"]
    assert result.crossings[0].budget_seconds == 600


def test_the_contract_names_the_heartbeat_topic_and_model() -> None:
    contract = yaml.safe_load(
        (_ROOT / "node_work_progress_compute/contract.yaml").read_text("utf-8")
    )
    (declared,) = contract["config"]["work_episodes"]["heartbeat_inputs"]
    assert declared["kind"] == "round"
    assert declared["request_field"] in ModelWorkEpisodeRequest.model_fields
    module, _, name = declared["model"].rpartition(".")
    assert getattr(import_module(module), name) is ModelScheduleRunEvent
    # The topic is the one the shipped scheduled-run reducer subscribes to.
    reducer = yaml.safe_load(
        (_ROOT / "node_schedule_run_reducer/contract.yaml").read_text("utf-8")
    )
    assert declared["topic"] in reducer["event_bus"]["subscribe_topics"]
    # The alarm hop is the alarm node's own command topic and input model.
    alarm = yaml.safe_load(
        (_ROOT / "node_lab_alarm_deliver_effect/contract.yaml").read_text("utf-8")
    )
    hop = contract["config"]["work_episodes"]["alarm_input"]
    assert hop["topic"] == alarm["runtime_dispatch"]["command_topic"]
    assert (
        hop["model"]
        == f"{alarm['input_model']['module']}.{alarm['input_model']['name']}"
    )
