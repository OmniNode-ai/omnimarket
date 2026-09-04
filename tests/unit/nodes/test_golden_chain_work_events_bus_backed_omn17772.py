# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the bus-backed work-events exposure (OMN-17772).

This extends the OMN-16180 chain past the durable row, through the seam the
row could not previously cross:

    real hook payload -> HandlerProjectionWorkEvents.handle
        -> InmemoryDatabaseAdapter row in omninode_internal.work_events
        -> encode_snapshot_delta keyed delta on
           onex.snapshot.projection.work.events.v1
        -> SnapshotCache.apply_message
        -> build_morning_page / render_morning_page

Every payload below is the same verbatim wire capture the OMN-16180 chain uses
(stability-lane broker, 2026-08-29/30), not a fixture invented to match the
handler.

The RED baseline these were written against, on origin/dev @ a0941e08:

* ``test_contract_is_discoverable`` failed — ``build_projection_topic_map``
  excluded ``projection_work_events`` outright because the contract declared
  ``schema: omninode_internal`` and ``ALLOWED_SCHEMAS`` is
  ``{public, omnidash_analytics}``. The exposure was absent from the catalog
  entirely; the page could not even refuse it.
* ``test_written_row_publishes_a_keyed_snapshot_delta`` failed — the exposure
  was not ``bus_backed``, ``HandlerProjectionWorkEvents`` had no publish call
  site, and the def-B DB-injection arm it runs on had no publisher to reach.
* ``test_page_renders_the_work_events_panel_from_the_bus`` failed — the page
  had no work-events panel at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_work_events.handlers.handler_projection_work_events import (
    TABLE,
    TOPIC_PROMPT_SUBMITTED,
    TOPIC_SESSION_ENDED,
    TOPIC_SESSION_STARTED,
    TOPIC_TOOL_EXECUTED,
    HandlerProjectionWorkEvents,
)
from omnimarket.projection.discovery import (
    ALLOWED_SCHEMAS,
    load_projection_exposures_from_contract,
)
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.morning_page import (
    TOPIC_WORK_EVENTS,
    EnumPanelState,
    build_morning_page,
    render_morning_page,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.snapshot_cache import SnapshotCache
from omnimarket.projection.snapshot_publisher import (
    ModelSnapshotDeltaMessage,
)

# The key-part delimiter, spelled here rather than imported: it is private to
# the publisher module on purpose, and a single-column key must never contain
# it, which is what the assertion below is for.
KEY_DELIMITER = "|"

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_work_events"
    / "contract.yaml"
)

_SESSION = "5bc4e084-6a53-4f69-936e-998985adbcf5"

# Verbatim bytes off the stability-lane broker (same capture as OMN-16180).
_WIRE: list[tuple[str, str]] = [
    (
        TOPIC_SESSION_STARTED,
        '{"session_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"working_directory": "omni_home", "hook_source": "startup", '
        '"correlation_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"causation_id": null, "emitted_at": "2026-08-29T22:18:47.100000+00:00", '
        '"entity_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"schema_version": "1.0.0"}',
    ),
    (
        TOPIC_PROMPT_SUBMITTED,
        '{"session_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"working_directory": "omni_home", "prompt_length": 412, '
        '"hook_source": "user_prompt_submit", '
        '"correlation_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"causation_id": null, "emitted_at": "2026-08-29T22:19:02.500000+00:00", '
        '"entity_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"schema_version": "1.0.0"}',
    ),
    (
        TOPIC_TOOL_EXECUTED,
        '{"session_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"working_directory": "omni_home", "tool_name": "Bash", '
        '"duration_ms": 184, "interrupted": false, '
        '"hook_source": "post_tool_use", '
        '"correlation_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"causation_id": null, "emitted_at": "2026-08-30T01:59:07.891697+00:00", '
        '"entity_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"schema_version": "1.0.0"}',
    ),
    (
        TOPIC_SESSION_ENDED,
        '{"session_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"reason": "clear", '
        '"correlation_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"causation_id": null, "emitted_at": "2026-08-30T02:10:00.000000+00:00", '
        '"entity_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"schema_version": "1.0.0"}',
    ),
]


class _RecordingPublisher:
    """Captures exactly what would go on the wire, byte for byte.

    Not a stand-in for the encoding: ``encode_snapshot_delta`` (OMN-17774) does
    the whole key/header/value construction and this only records the send, so
    every assertion below lands on the real wire shape rather than on a
    re-implementation of it. Implements ``ProtocolSnapshotDeltaPublisher``, the
    seam the handler takes by constructor injection, so no module attribute is
    patched to make this work.
    """

    def __init__(self, *, accept: bool = True) -> None:
        self.sent: list[tuple[str, bytes | None, bytes, list[tuple[str, bytes]]]] = []
        self._accept = accept

    def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        self.sent.append(
            (message.topic, message.value, message.key, list(message.headers))
        )
        return self._accept


def _exposure() -> ProjectionTableConfig:
    contract: dict[str, Any] = yaml.safe_load(_CONTRACT_PATH.read_text())
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), _CONTRACT_PATH
    )
    assert exposures, "the contract must still declare a projection_api exposure"
    return exposures[0]


def _project_all(handler: HandlerProjectionWorkEvents, db: InmemoryDatabaseAdapter):
    for topic, raw in _WIRE:
        handler.handle({**json.loads(raw), "_db": db, "_topic": topic})


def test_contract_declares_a_servable_schema_and_a_content_addressed_key() -> None:
    """RED on origin/dev: the contract was excluded at discovery.

    ``schema: omninode_internal`` is not in ``ALLOWED_SCHEMAS``, so
    ``build_projection_topic_map`` dropped this contract before the catalog was
    built and the exposure appeared nowhere — not as data, not as a refusal.
    """
    exposure = _exposure()
    assert exposure.schema_name in ALLOWED_SCHEMAS
    assert exposure.topic == TOPIC_WORK_EVENTS
    assert exposure.bus_backed is True
    # The key is what makes constant ordering coordinates safe on the
    # DB-injection arm: event_id is content-addressed, so a repeated key is a
    # byte-identical replay and there is no intra-key ordering to preserve.
    assert exposure.key_columns == ("event_id",)


def test_written_row_publishes_a_keyed_snapshot_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED on origin/dev: nothing published; the exposure was not bus_backed."""
    producer = _RecordingPublisher()

    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionWorkEvents(publisher=producer)
    _project_all(handler, db)

    rows = db.query(TABLE)
    assert len(rows) == 4, "the durable write is unchanged by the publish"
    assert len(producer.sent) == 4, "one delta per written row, published after"

    by_event_id = {str(row["event_id"]): row for row in rows}
    for topic, value, key, headers in producer.sent:
        assert topic == TOPIC_WORK_EVENTS
        assert value is not None, "an upsert is never a tombstone"
        event_id = key.decode("utf-8")
        assert KEY_DELIMITER not in event_id, "single-column key, no delimiter"
        assert event_id in by_event_id, "the key IS the row's content address"

        delta = json.loads(value)
        assert delta["op"] == "upsert"
        assert delta["topic"] == TOPIC_WORK_EVENTS
        assert delta["key"] == [event_id]
        assert delta["source_topic"] in {t for t, _ in _WIRE}
        assert delta["source_event_id"] == _SESSION
        row = delta["row"]
        assert row["event_id"] == event_id
        assert row["actor_id"] == _SESSION
        assert row["actor_kind"] == "session"
        # The published row is the row that was written, not a re-derivation.
        assert row["summary"] == by_event_id[event_id]["summary"]
        assert row["event_kind"] == by_event_id[event_id]["event_kind"]
        assert dict(headers)["content_type"] == b"application/json"
        assert dict(headers)["schema_version"] == b"projection_snapshot.v1"

    kinds = {json.loads(v)["row"]["event_kind"] for _, v, _, _ in producer.sent}
    assert kinds == {
        "session.started",
        "session.prompt",
        "session.tool",
        "session.ended",
    }


def test_a_failed_write_publishes_nothing() -> None:
    """A delta is never published for a row the table did not accept.

    Publishing regardless would put a work event on the read model's topic that
    the ledger cannot produce — the read model would show activity the durable
    record denies.
    """

    class _RefusingAdapter(InmemoryDatabaseAdapter):
        def upsert(self, table: str, conflict_key: str, row: dict[str, Any]) -> bool:
            return False

    producer = _RecordingPublisher()
    handler = HandlerProjectionWorkEvents(publisher=producer)
    topic, raw = _WIRE[0]
    result = handler.handle(
        {**json.loads(raw), "_db": _RefusingAdapter(), "_topic": topic}
    )
    assert result["rows_upserted"] == 0
    assert producer.sent == [], (
        "the publish seam must not be reached at all when the table refused"
    )


def test_page_renders_the_work_events_panel_from_the_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED on origin/dev: there was no work-events panel to render.

    The full chain: the four captured hook payloads become rows, the rows
    become deltas on the snapshot topic, the cache applies the raw wire bytes,
    and the page renders what the cache holds. No step is stubbed except the
    broker itself.
    """
    producer = _RecordingPublisher()

    exposure = _exposure()
    db = InmemoryDatabaseAdapter()
    _project_all(HandlerProjectionWorkEvents(publisher=producer), db)

    cache = SnapshotCache(
        {exposure.topic: exposure},
        bootstrap_servers="unused-in-this-test:9092",
        # Explicit, because the default derives the group id from
        # os.environ["ONEX_ENVIRONMENT"] and raises KeyError when it is unset.
        # Leaving it implicit made this test pass on a shell that happened to
        # export it and fail on a lab host that did not -- a test whose verdict
        # depends on the ambient environment is not evidence of anything.
        group_id="omn17772-golden-chain",
    )
    for topic, value, key, headers in producer.sent:
        cache.apply_message(topic, key, value, headers)
    # A real cache, driven through its real apply path; bootstrap is the only
    # thing a live consumer loop would set, so set it the same way the loop
    # does rather than stubbing the cache.
    cache._state[exposure.topic].bootstrap_complete = True

    assert cache.row_count(exposure.topic) == 4

    page = build_morning_page(
        {exposure.topic: exposure},
        cache,
        service_name="omnimarket-projection-api",
    )
    assert page.work_events.state is EnumPanelState.LIVE
    assert page.work_events.cached_row_count == 4
    assert len(page.work_events.rows) == 4
    assert page.bus_backed_count == 1

    html = render_morning_page(page)
    assert "work events" in html
    # The panel itself must not be a refusal. The census panel's own prose
    # explains the refusal code, so assert on the work-events SECTION, not on
    # the whole document.
    section = html.split("<span>work events</span>", 1)[1].split("</section>", 1)[0]
    assert "REFUSED" not in section
    assert "not_yet_bus_backed" not in section
    # The rendered rows are the measured ones, named individually.
    assert "tool Bash (184 ms)" in html
    assert "prompt submitted (412 chars)" in html
    assert "session ended (clear)" in html
    assert "session started in omni_home" in html


def test_replaying_an_identical_delta_does_not_duplicate_a_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The constant ordering coordinates are safe because the key is a content
    address.

    A second delivery of the same event carries the same key AND the same
    bytes, so the cache dropping it as an idempotent replay leaves the correct
    row in place. This is the property the DB-injection arm relies on, and it
    is asserted rather than assumed.
    """
    producer = _RecordingPublisher()

    exposure = _exposure()
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionWorkEvents(publisher=producer)
    _project_all(handler, db)
    _project_all(handler, db)

    cache = SnapshotCache(
        {exposure.topic: exposure},
        bootstrap_servers="unused-in-this-test:9092",
        # Explicit, because the default derives the group id from
        # os.environ["ONEX_ENVIRONMENT"] and raises KeyError when it is unset.
        # Leaving it implicit made this test pass on a shell that happened to
        # export it and fail on a lab host that did not -- a test whose verdict
        # depends on the ambient environment is not evidence of anything.
        group_id="omn17772-golden-chain",
    )
    for topic, value, key, headers in producer.sent:
        cache.apply_message(topic, key, value, headers)

    assert len(producer.sent) == 8, "the writer republished all four"
    assert cache.row_count(exposure.topic) == 4, "the cache still holds four keys"
    assert len(db.query(TABLE)) == 4, "and the table still holds four rows"


def test_publish_failure_does_not_fail_the_durable_write(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broker outage must not make a landed row look unwritten.

    The runtime's terminal event asserts that a durable row landed; reporting
    zero rows because the read model lagged would be the opposite lie.
    """

    # A transport that is reachable but refuses -- exactly what
    # KafkaSnapshotDeltaPublisher returns when no broker answers. It returns
    # False rather than raising, which is the contract the protocol declares.
    producer = _RecordingPublisher(accept=False)

    db = InmemoryDatabaseAdapter()
    topic, raw = _WIRE[2]
    result = HandlerProjectionWorkEvents(publisher=producer).handle(
        {**json.loads(raw), "_db": db, "_topic": topic}
    )
    assert result["rows_upserted"] == 1
    assert len(db.query(TABLE)) == 1
    assert len(producer.sent) == 1, "the delta was offered to the transport"
    assert "snapshot delta not published" in caplog.text


def test_the_published_row_survives_a_json_round_trip_with_its_timestamp() -> None:
    """``emitted_at`` is written as a real datetime and must serialize.

    asyncpg refuses a str for a TIMESTAMPTZ, so the row carries a
    ``datetime``; the snapshot value is JSON, so the publisher has to convert
    it. A row that writes fine and fails to publish is exactly the split-brain
    this exposure exists to close.
    """
    handler = HandlerProjectionWorkEvents(publisher=_RecordingPublisher())
    from omnimarket.nodes.node_projection_work_events.models.model_work_event import (
        ModelWorkEventInbound,
    )

    topic, raw = _WIRE[0]
    row_dict = handler._row_dict(
        handler.accumulate(ModelWorkEventInbound(**json.loads(raw)), topic)
    )
    assert isinstance(row_dict["emitted_at"], datetime)
    assert row_dict["emitted_at"].tzinfo is not None

    from omnimarket.projection.models import snapshot_json_value

    serialized = snapshot_json_value(row_dict["emitted_at"])
    assert isinstance(serialized, str)
    assert datetime.fromisoformat(serialized).astimezone(UTC).year == 2026
