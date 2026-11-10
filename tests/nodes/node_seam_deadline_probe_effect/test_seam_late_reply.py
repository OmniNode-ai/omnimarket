# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A seam that misses its deadline is listened to a bounded time longer (base-first piece 3, OMN-23418).

Golden: an in-time reply records the send acknowledgement and reads ``ontime``. Error: a reply planted after the
deadline but inside the listen window still reads NOT_WORKING / no-result-by-deadline, and the row says ``late`` with
how late; a reply that never comes reads ``silent``, so a responder stall reads apart from no reply. The failing
control is test 2: on the probe before this change the row has no reply cell and a late reply reads like no reply.
The kafka adapter runs over a fake transport so its listen loop is read, not assumed.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.adapters.inmemory_seam_bus import (
    InMemorySeamBus,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.adapters.kafka_seam_bus import (
    KafkaSeamBus,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.models.model_seam_deadline import (
    EnumSeamCrossing,
    ModelSeamDemand,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.overlay import (
    ModelSeamDeadlineOverlay,
)
from tests.nodes.node_seam_deadline_probe_effect.test_seam_deadline_chain_events import (
    _EXAMPLE,
    _answering_bus,
    _handler,
    _overlay,
    _run,
)

pytestmark = pytest.mark.unit

SEAM = "example-query"


def _row_after(bus: InMemorySeamBus) -> tuple[str, list[tuple[str, dict[str, object]]]]:
    ledger = InMemoryLedger()
    events = _run(_handler(bus, ledger))
    return ledger.lines[0], events


def _late_bus(latency_ms: int) -> InMemorySeamBus:
    bus = _answering_bus(_overlay())
    bus.latency_ms = latency_ms
    return bus


def _silent_bus() -> InMemorySeamBus:
    seam = _overlay().seams[0]
    return InMemorySeamBus({seam.command_topic: lambda _p: None})


def test_golden_chain_an_in_time_reply_records_the_ack_and_reads_ontime() -> None:
    row, events = _row_after(_answering_bus(_overlay()))
    assert f"seam_{SEAM}_reply=ontime" in row
    assert f"seam_{SEAM}_send_ack_ms=2" in row
    assert f"seam_{SEAM}_late_ms=none" in row
    assert f"seam_{SEAM}_listen_ms=60000" in row
    assert f"seam_{SEAM}=WORKING:result:5ms/30000ms" in row
    assert events[-1][0].endswith("seam-deadline-run-completed.v1")
    assert events[1][1]["reply"] == "ontime"
    assert events[1][1]["send_ack_ms"] == 2


def test_error_chain_a_planted_late_reply_reads_late_not_silent() -> None:
    """The failing control: the unfixed probe writes no reply cell, so this late reply reads like no reply."""
    row, events = _row_after(_late_bus(45_000))
    assert events[-1][0].endswith("seam-deadline-run-failed.v1")
    assert events[1][1]["crossing"] == "no-result-by-deadline"
    assert events[1][1]["reply"] == "late"
    assert events[1][1]["late_reply_ms"] == 45_000
    assert f"seam_{SEAM}=NOT_WORKING:no-result-by-deadline:" in row
    assert f"seam_{SEAM}_reply=late" in row
    assert f"seam_{SEAM}_late_ms=45000" in row
    assert f"seam_{SEAM}_send_ack_ms=2" in row


def test_error_chain_no_reply_inside_the_listen_window_reads_silent() -> None:
    row, _ = _row_after(_silent_bus())
    assert f"seam_{SEAM}_reply=silent" in row
    assert f"seam_{SEAM}_late_ms=none" in row
    assert "verdict=NOT_WORKING" in row


def test_error_chain_a_reply_past_the_listen_window_reads_silent() -> None:
    row, _ = _row_after(_late_bus(120_000))
    assert f"seam_{SEAM}_reply=silent" in row
    assert f"seam_{SEAM}_late_ms=none" in row


def test_a_late_reply_and_no_reply_leave_different_rows() -> None:
    late, _ = _row_after(_late_bus(45_000))
    none, _ = _row_after(_silent_bus())

    def cell(row: str, key: str) -> str:
        return next(c for c in row.split(" | ") if c.startswith(f"{key}="))

    assert cell(late, f"seam_{SEAM}_reply") != cell(none, f"seam_{SEAM}_reply")
    assert cell(late, f"seam_{SEAM}_late_ms") != cell(none, f"seam_{SEAM}_late_ms")
    assert cell(late, "verdict") == cell(none, "verdict")
    assert cell(late, "reason") == cell(none, "reason")


def _overlay_with_listen(seconds: float) -> ModelSeamDeadlineOverlay:
    document = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    document["seams"][0]["late_listen_seconds"] = seconds
    return ModelSeamDeadlineOverlay.model_validate(document)


def test_a_seam_with_no_late_listen_reads_unlistened() -> None:
    from omnimarket.nodes.node_seam_deadline_probe_effect.handlers.handler_seam_deadline import (
        HandlerSeamDeadline,
    )
    from omnimarket.nodes.node_seam_reading_compute.handlers.handler_required_sender import (
        HandlerRequiredSender,
    )
    from tests.nodes.node_seam_deadline_probe_effect.test_seam_deadline_chain_events import (
        _NOW,
    )

    ledger = InMemoryLedger()
    handler = HandlerSeamDeadline(
        overlay=_overlay_with_listen(0),
        host="test-host",
        bus=_late_bus(45_000),
        appender=ledger,
        clock=lambda: _NOW,
        required_sender=HandlerRequiredSender(),
    )
    _run(handler)
    row = ledger.lines[0]
    assert f"seam_{SEAM}_reply=unlistened" in row
    assert f"seam_{SEAM}_listen_ms=0" in row
    assert f"seam_{SEAM}_late_ms=none" in row


def test_error_an_unreadable_bus_has_no_reply_state() -> None:
    bus = _answering_bus(_overlay())
    bus.unreadable = "topicauthorizationfailed"
    row, _ = _row_after(bus)
    assert f"seam_{SEAM}_reply=none" in row
    assert f"seam_{SEAM}_send_ack_ms=none" in row


def test_error_the_overlay_refuses_a_seam_with_no_late_listen_setting() -> None:
    document = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    del document["seams"][0]["late_listen_seconds"]
    with pytest.raises(ValidationError, match="late_listen_seconds"):
        ModelSeamDeadlineOverlay.model_validate(document)


def test_a_late_reply_does_not_change_the_deadline_or_the_verdict() -> None:
    row, events = _row_after(_late_bus(45_000))
    assert "ms/30000ms" in row
    assert events[-1][1]["verdict"] == "NOT_WORKING"


# -- the kafka adapter's listen loop, over a fake transport ---------------------------------------


class _FakeTransport:
    """A transport whose reply, if any, comes ``reply_after`` seconds after the send."""

    reply_after: float | None = None

    def __init__(self, **_kwargs: object) -> None:
        self._sent: dict[str, object] | None = None
        self._sent_at = 0.0
        self._answered = False

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def send(
        self, topic: str, key: bytes, value: bytes, headers: dict[str, bytes]
    ) -> None:
        self._sent = json.loads(value)
        self._sent_at = time.monotonic()
        self._sent_wall = datetime.now(UTC)

    async def poll(self, *, max_messages: int, timeout_ms: int) -> list[object]:
        assert self._sent is not None
        due = self.reply_after is not None and (
            time.monotonic() - self._sent_at >= self.reply_after
        )
        if due and not self._answered:
            self._answered = True
            stamp = self._sent_wall
            body = {
                "correlation_id": self._sent["correlation_id"],
                "envelope_timestamp": (stamp + timedelta(milliseconds=500)).isoformat(),
                "payload": {"rows": []},
            }
            return [SimpleNamespace(value=json.dumps(body).encode())]
        await asyncio.sleep(min(timeout_ms / 1000, 0.01))
        return []


def _kafka_demand(listen: float) -> ModelSeamDemand:
    return ModelSeamDemand(
        seam=SEAM,
        command_topic="onex.cmd.example.query-requested.v1",
        result_topic="onex.evt.example.query-completed.v1",
        consumer_group_prefix="local.example.query_client.consume.v1.",
        correlation_id=str(uuid4()),
        payload={"query": "holds"},
        error_keys=("error",),
        deadline_seconds=0.2,
        late_listen_seconds=listen,
    )


def _kafka(monkeypatch: pytest.MonkeyPatch, reply_after: float | None) -> KafkaSeamBus:
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

    monkeypatch.setattr(
        "omnibase_infra.event_bus.kafka_transport.KafkaTransport", _FakeTransport
    )
    monkeypatch.setattr(_FakeTransport, "reply_after", reply_after)
    monkeypatch.setattr(
        KafkaSeamBus, "_config", lambda _self: ModelKafkaEventBusConfig.default()
    )
    return KafkaSeamBus("dev")


def test_kafka_adapter_an_in_time_reply_has_the_ack_and_the_reply_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = _kafka(monkeypatch, 0.0).demand(_kafka_demand(1.0))
    assert answer.crossing is EnumSeamCrossing.RESULT
    assert answer.send_ack_ms is not None
    assert answer.reply_stamp_ms is not None
    assert 450 <= answer.reply_stamp_ms <= 550


def test_kafka_adapter_a_reply_after_the_deadline_is_late_and_not_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = _kafka(monkeypatch, 0.5).demand(_kafka_demand(1.0))
    assert answer.crossing is EnumSeamCrossing.NO_RESULT_BY_DEADLINE
    assert answer.detail == "late"
    assert answer.late_reply_ms is not None
    assert answer.late_reply_ms >= 200
    assert answer.send_ack_ms is not None


def test_kafka_adapter_no_reply_in_the_listen_window_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = _kafka(monkeypatch, None).demand(_kafka_demand(0.4))
    assert answer.crossing is EnumSeamCrossing.NO_RESULT_BY_DEADLINE
    assert answer.detail == "silent"
    assert answer.late_reply_ms is None


def test_kafka_adapter_with_no_listen_a_late_reply_is_never_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = _kafka(monkeypatch, 0.5).demand(_kafka_demand(0.0))
    assert answer.crossing is EnumSeamCrossing.NO_RESULT_BY_DEADLINE
    assert answer.detail == ""
    assert answer.late_reply_ms is None
