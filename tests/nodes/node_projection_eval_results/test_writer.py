# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24171 AC4 (writer): a tick republishes with a new publish time and unchanged run times; a refusal loses no item."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from omnimarket.nodes.node_projection_eval_results.handlers.handler_eval_results_writer import (
    EvalResultsProjectionWriter,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    SOURCE_TOPICS,
    TOPIC_CODE_TRY,
    TOPIC_JUDGED,
    TOPIC_RUN_CLOSED,
    TOPIC_SNAPSHOT,
    TOPIC_TICK,
)
from tests.nodes.node_projection_eval_results.support import (
    MODEL,
    TENANT,
    MemoryStore,
    at,
    close_event,
    code_event,
    deliver,
    judged_event,
    make_writer,
    overlay,
    recorded_judged_render_result,
    try_row,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000a1"


async def _tracked_run(writer: Any) -> None:
    await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, try_row(RUN, "t1")), at(2))
    await deliver(
        writer, TOPIC_RUN_CLOSED, close_event(RUN, (("t1", MODEL, "S"),)), at(3)
    )


def test_the_writer_declares_in_process_dispatch_and_the_contract_topics() -> None:
    assert EvalResultsProjectionWriter.onex_runtime_inprocess_dispatch is True
    writer = EvalResultsProjectionWriter(overlay=overlay())
    assert set(writer.subscribe_topics) == {*SOURCE_TOPICS, TOPIC_TICK}


async def test_a_tick_republishes_with_a_new_publish_time_and_unchanged_run_times() -> (
    None
):
    store = MemoryStore()
    writer, published = make_writer(store, clock=lambda: at(100))
    await _tracked_run(writer)
    before = published[-1][1]
    count = len(published)
    outcome = await deliver(
        writer, TOPIC_TICK, {"now": at(100 + 11).isoformat()}, at(111)
    )
    assert outcome["snapshot"]["published"] is True
    topic, after = published[-1]
    assert topic == TOPIC_SNAPSHOT
    assert len(published) == count + 1
    assert after["published_at"] != before["published_at"]
    assert after["published_at"] == "2026-11-09T13:51:00Z"
    assert after["models"] == before["models"], "a republish must not move any run time"
    (run,) = after["models"][0]["runs"]
    assert run["last_scored_at"] == "2026-11-09T12:02:00Z"


async def test_a_tick_inside_the_publish_interval_publishes_nothing() -> None:
    store = MemoryStore()
    writer, published = make_writer(store, clock=lambda: at(100))
    await _tracked_run(writer)
    count = len(published)
    outcome = await deliver(
        writer,
        TOPIC_TICK,
        {"now": (at(100) + timedelta(seconds=599)).isoformat()},
        at(110),
    )
    assert outcome["snapshot"] == {"published": False, "reason": "interval"}
    assert len(published) == count


async def test_a_tick_publishes_even_when_no_event_ever_arrived() -> None:
    store = MemoryStore()
    writer, published = make_writer(store)
    await deliver(writer, TOPIC_TICK, {"now": at(5).isoformat()}, at(5))
    ((_, snapshot),) = published
    assert snapshot["published_at"] == "2026-11-09T12:05:00Z"
    assert snapshot["models"] == [{"model_id": MODEL, "runs": [], "live_weeks": []}]


async def test_a_refused_snapshot_is_not_published_and_loses_no_item() -> None:
    store = MemoryStore()
    writer, published = make_writer(store, overlay_=overlay(snapshot_max_bytes=50))
    outcome = await deliver(
        writer, TOPIC_CODE_TRY, code_event(RUN, try_row(RUN, "t1")), at(2)
    )
    assert outcome["snapshot"]["published"] is False
    assert outcome["snapshot"]["refused"] == "over_bound"
    assert outcome["snapshot"]["size"] > outcome["snapshot"]["limit"] == 50
    assert published == []
    assert len(store.items) == 1


async def test_the_lane_stamps_come_from_the_overlay() -> None:
    store = MemoryStore()
    writer, _ = make_writer(
        store, overlay_=overlay(lane="stability", host="h101", install_revision="rev-9")
    )
    await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, try_row(RUN, "t1")), at(2))
    (item,) = store.items.values()
    assert (item.lane.value, item.host, item.install_revision) == (
        "stability",
        "h101",
        "rev-9",
    )
    assert item.tenant_id == TENANT


async def test_an_event_with_no_envelope_time_is_refused_not_stamped_with_a_clock() -> (
    None
):
    from omnimarket.projection.runner import MessageMeta

    store = MemoryStore()
    writer, _ = make_writer(store)
    with pytest.raises(ValueError, match="event time"):
        await writer._project(
            TOPIC_CODE_TRY,
            {**code_event(RUN, try_row(RUN, "t1")), "_tenant_id": TENANT},
            MessageMeta(partition=0, offset=0, fallback_id="", topic=TOPIC_CODE_TRY),
        )
    assert store.items == {}


async def test_a_malformed_delivery_goes_to_the_contract_dlq_topic_and_stores_nothing() -> (
    None
):
    store = MemoryStore()
    writer, published = make_writer(store)
    bad_rows = {"run_id": RUN, "rows": [{"run_id": RUN}]}
    outcome = await deliver(writer, TOPIC_CODE_TRY, bad_rows, at(2))
    assert outcome["dlq_routed"] == 1
    assert store.items == {}
    ((topic, envelope),) = published
    assert topic == "onex.dlq.omnimarket.projection-eval-results-malformed.v1"
    assert "invalid_event" in json.dumps(envelope)


async def test_a_control_row_is_skipped_by_design_not_sent_to_the_dlq() -> None:
    store = MemoryStore()
    writer, published = make_writer(store)
    control = try_row(RUN, "t1", row_kind="control")
    outcome = await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, control), at(2))
    assert outcome["dlq_routed"] == 0
    assert [r["reason"] for r in outcome["refused"]] == ["not_a_try"]
    assert store.items == {}
    assert all(topic == TOPIC_SNAPSHOT for topic, _ in published)


DLQ_TOPIC = "onex.dlq.omnimarket.projection-eval-results-malformed.v1"
CALL = "00000000-0000-0000-0000-00000000c001"


async def test_a_judged_delivery_with_no_judge_run_id_goes_to_the_dlq_and_is_counted() -> (
    None
):
    store = MemoryStore()
    writer, published = make_writer(store)
    payload = {k: v for k, v in judged_event(CALL).items() if k != "judge_run_id"}
    outcome = await deliver(writer, TOPIC_JUDGED, payload, at(2))
    assert outcome["dlq_routed"] == 1
    assert [r["reason"] for r in outcome["refused"]] == ["invalid_event"]
    assert "judge_run_id" in outcome["refused"][0]["detail"]
    assert store.items == {}
    ((topic, envelope),) = published
    assert topic == DLQ_TOPIC
    assert "invalid_event" in json.dumps(envelope)


async def test_a_judged_delivery_of_the_recorded_dev_broker_event_goes_to_the_dlq() -> (
    None
):
    store = MemoryStore()
    writer, published = make_writer(store)
    outcome = await deliver(
        writer, TOPIC_JUDGED, recorded_judged_render_result(), at(2)
    )
    assert outcome["dlq_routed"] == 1
    assert [r["reason"] for r in outcome["refused"]] == ["invalid_event"]
    assert store.items == {}
    assert [topic for topic, _ in published] == [DLQ_TOPIC]


async def test_a_valid_judged_delivery_routes_nothing_to_the_dlq_control() -> None:
    store = MemoryStore()
    writer, published = make_writer(store)
    outcome = await deliver(writer, TOPIC_JUDGED, judged_event(CALL), at(2))
    assert outcome["dlq_routed"] == 0
    assert outcome["refused"] == []
    assert len(store.items) == 1
    assert all(topic != DLQ_TOPIC for topic, _ in published)


@pytest.mark.parametrize("bad_tenant", ["omninode", "not-a-uuid", "0" * 31])
@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        (TOPIC_JUDGED, judged_event(CALL)),
        (TOPIC_CODE_TRY, code_event(RUN, try_row(RUN, "t1"))),
    ],
)
async def test_a_delivery_whose_envelope_tenant_is_not_a_uuid_is_dead_lettered_typed(
    bad_tenant: str, topic: str, payload: dict[str, Any]
) -> None:
    """The tenant stamp is part of the delivery: one that is no uuid is refused once, never raised and rewound."""
    store = MemoryStore()
    writer, published = make_writer(store)
    outcome = await deliver(writer, topic, payload, at(2), tenant=bad_tenant)
    assert outcome["rows_upserted"] == 0
    assert outcome["dlq_routed"] == 1
    assert [r["reason"] for r in outcome["refused"]] == ["invalid_event"]
    assert "tenant" in outcome["refused"][0]["detail"]
    assert store.items == {}
    assert [t for t, _ in published] == [DLQ_TOPIC]


async def test_a_delivery_whose_envelope_tenant_is_not_a_uuid_raises_when_the_dead_letter_is_not_published() -> (
    None
):
    store = MemoryStore()
    writer, _ = make_writer(store)

    async def refuse(topic: str, value: bytes) -> None:
        raise ConnectionError("broker down")

    writer._publish_fn = refuse
    with pytest.raises(RuntimeError, match="dead letter not published"):
        await deliver(
            writer, TOPIC_JUDGED, judged_event(CALL), at(2), tenant="omninode"
        )
    assert store.items == {}


async def test_a_configured_tenant_that_is_not_a_uuid_still_raises_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane whose own tenant setting is no uuid is a defect of the lane, not of the event: the offset stays."""
    monkeypatch.setattr(
        "omnimarket.nodes.node_projection_eval_results.handlers.handler_eval_results_writer.house_tenant_write_stamp",
        lambda **_: {"tenant_id": "omninode"},
    )
    writer, published = make_writer(MemoryStore())
    with pytest.raises(ValueError, match="badly formed hexadecimal UUID string"):
        await deliver(writer, TOPIC_JUDGED, judged_event(CALL), at(2), tenant="")
    assert published == []
