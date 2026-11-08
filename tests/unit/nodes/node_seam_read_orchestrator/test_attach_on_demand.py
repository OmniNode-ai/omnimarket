# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Attach-on-demand activation of the seam read (base-first piece 3, OMN-22909).

A command node with an on-demand path only has no consumer on the bus until something attaches it.
The activation attaches one such node, sends its command, reads the correlated result before the
seam's deadline and detaches it. Golden: an attachable node answers in time and the seam reads
WORKING. Error chain: the demanded command gets no result by its deadline (the plan's named break
for piece 3), the node cannot be attached, or nothing is attached at all: each ends in the typed
failure event, never silence.
"""

from __future__ import annotations

import json

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.seam_reading.model_attach_on_demand import (
    ModelAttachOnDemandRequest,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.adapters.inmemory_seam_bus import (
    InMemorySeamBus,
    Responder,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.models.model_seam_deadline import (
    EnumSeamCrossing,
    ModelSeamDemand,
)
from omnimarket.nodes.node_seam_read_orchestrator.adapters.inmemory_node_attacher import (
    InMemoryNodeAttacher,
)
from omnimarket.nodes.node_seam_read_orchestrator.handlers.handler_attach_on_demand import (
    HandlerAttachOnDemand,
    answer_command,
    load_topics,
)

pytestmark = pytest.mark.unit

COMMAND_TOPIC = "onex.cmd.example.on-demand-requested.v1"
RESULT_TOPIC = "onex.evt.example.on-demand-completed.v1"


def _request(**overrides: object) -> ModelAttachOnDemandRequest:
    fields: dict[str, object] = {
        "correlation_id": "attach-1",
        "seam": "example-on-demand",
        "command_topic": COMMAND_TOPIC,
        "result_topic": RESULT_TOPIC,
        "consumer_group_prefix": "local.example.client.consume.v1.",
        "payload": {"query": "holds"},
        "error_keys": ("error",),
        "deadline_seconds": 30.0,
    }
    fields.update(overrides)
    return ModelAttachOnDemandRequest.model_validate(fields)


def _answer(payload: dict[str, str | int | bool]) -> dict[str, object] | None:
    return {"query": payload["query"], "rows": []}


def _wiring(
    node: Responder = _answer, *, latency_ms: int = 5
) -> tuple[HandlerAttachOnDemand, InMemorySeamBus, InMemoryNodeAttacher]:
    bus = InMemorySeamBus(latency_ms=latency_ms)
    attacher = InMemoryNodeAttacher(bus, available={COMMAND_TOPIC: node})
    return HandlerAttachOnDemand(attacher=attacher, bus=bus), bus, attacher


def test_attaches_one_on_demand_node_sends_its_command_and_reads_the_result() -> None:
    handler, bus, attacher = _wiring()
    assert (
        COMMAND_TOPIC not in bus.responders
    )  # on-demand only: nothing consumes it yet

    result = handler.handle(_request())

    assert result.status == "completed"
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.crossing is EnumSeamCrossing.RESULT
    assert result.attached is True
    assert result.elapsed_ms is not None
    assert result.elapsed_ms <= 30_000  # before the seam's deadline
    assert [d.command_topic for d in bus.sent] == [COMMAND_TOPIC]
    assert bus.sent[0].correlation_id == "attach-1"
    assert result.detached is True
    assert attacher.attached == ()  # the activation leaves the bus as it found it
    assert COMMAND_TOPIC not in bus.responders


def test_a_demanded_command_with_no_result_by_its_deadline_reads_not_working() -> None:
    # the failing control: the attached node is too slow for the deadline
    handler, _, attacher = _wiring(latency_ms=5_000)

    result = handler.handle(_request(deadline_seconds=1.0))

    assert result.status == "failed"
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.crossing is EnumSeamCrossing.NO_RESULT_BY_DEADLINE
    assert result.failure == "seam-not-crossed:example-on-demand:no-result-by-deadline"
    assert attacher.attached == ()  # detached even on failure


def test_without_the_attach_step_the_same_command_gets_no_result() -> None:
    # the control for the gap itself: an on-demand node nobody attached never answers
    bus = InMemorySeamBus()
    answer = bus.demand(
        ModelSeamDemand(
            seam="example-on-demand",
            command_topic=COMMAND_TOPIC,
            result_topic=RESULT_TOPIC,
            consumer_group_prefix="local.example.client.consume.v1.",
            correlation_id="attach-1",
            payload={"query": "holds"},
            error_keys=("error",),
            deadline_seconds=30.0,
        )
    )
    assert answer.crossing is EnumSeamCrossing.NO_RESULT_BY_DEADLINE


def test_a_node_that_cannot_be_attached_is_a_typed_failure_naming_it() -> None:
    handler, bus, attacher = _wiring()
    attacher.attach_fails = "node-not-loaded"

    result = handler.handle(_request())

    assert result.status == "failed"
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.attached is False
    assert result.crossing is EnumSeamCrossing.ATTACH_FAILED
    assert result.failure == "attach-failed:example-on-demand:node-not-loaded"
    assert bus.sent == []  # nothing was sent to a node that is not there


def test_an_unknown_command_topic_is_refused_not_attached() -> None:
    handler, bus, _ = _wiring()

    result = handler.handle(_request(command_topic="onex.cmd.example.other.v1"))

    assert result.status == "failed"
    assert result.attached is False
    assert result.failure == "attach-failed:example-on-demand:no-node-for-topic"
    assert bus.sent == []


def test_an_error_answer_reads_not_working_and_still_detaches() -> None:
    handler, _, attacher = _wiring(node=lambda _payload: {"error": "boom"})

    result = handler.handle(_request())

    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.crossing is EnumSeamCrossing.RESULT_ERROR
    assert attacher.attached == ()


def test_golden_chain_events_end_in_the_completed_event() -> None:
    topics = load_topics()
    handler, _, _ = _wiring()

    out = answer_command(_request().model_dump_json().encode(), handler, topics)

    events = [(topic, json.loads(body)) for topic, body in out]
    assert [t for t, _ in events] == [
        topics["attach_on_demand_started"],
        topics["on_demand_node_attached"],
        topics["attach_on_demand_completed"],
    ]
    assert all(body["correlation_id"] == "attach-1" for _, body in events)
    assert events[-1][1]["verdict"] == "WORKING"


def test_error_chain_events_end_in_the_failed_event() -> None:
    topics = load_topics()
    handler, _, _ = _wiring(latency_ms=5_000)

    out = answer_command(
        _request(deadline_seconds=1.0).model_dump_json().encode(), handler, topics
    )

    events = [(topic, json.loads(body)) for topic, body in out]
    assert events[-1][0] == topics["attach_on_demand_failed"]
    assert events[-1][1]["failure"].startswith("seam-not-crossed:example-on-demand")
    assert events[-1][1]["verdict"] == "NOT_WORKING"


def test_error_chain_a_bad_payload_is_a_typed_failure_never_dropped() -> None:
    topics = load_topics()
    handler, _, _ = _wiring()

    out = answer_command(b'{"seam": ""}', handler, topics)

    assert len(out) == 1
    assert out[0][0] == topics["attach_on_demand_failed"]
    assert json.loads(out[0][1])["failure"].startswith("bad-payload:")
