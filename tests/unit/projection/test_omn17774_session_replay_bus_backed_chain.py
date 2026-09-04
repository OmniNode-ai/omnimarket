# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17774 -- the session-replay exposure, end to end on the bus.

GOAL row 0 leg (b)/(c), epic OMN-16776, group G3.

THE DEFECT THIS CLOSES
----------------------
Measured on the .201 dev lane 2026-09-03T15:2x-15:3xZ, read-only:
``public.session_replay_snapshots`` holds 194 live rows across 9 sessions
(``pg_stat_user_tables.n_live_tup``, which RLS does not filter; the relation is
``relrowsecurity=f`` with zero policies) and the consumer group
``local.omnimarket.projection_session_replay.consume.1.0.0`` is Stable on all
five declared subscribe topics. The writer works. And the morning page renders,
verbatim::

    sessions  onex.snapshot.projection.session.replay.v1
    REFUSED: not_yet_bus_backed - tracked by OMN-15800

...because the contract declared neither ``bus_backed`` nor ``key_columns``, and
nothing republished a materialized row onto the snapshot topic the projection
API's ``SnapshotCache`` reads. The API holds no database handle by design, so a
row that is never republished is a row it can never serve, however durable it is
in Postgres.

RED BEFORE THE FIX (recorded pre-implementation): every test below except the
contract guard failed, because ``HandlerProjectionSessionReplay.handle`` wrote a
row and published nothing -- ``ModelProjectionReplayResult`` carried only
``rows_upserted`` and there was no publish seam a sync projection handler could
reach at all.

WHAT IS ASSERTED
----------------
The whole chain, in the same order the runtime executes it: contract -> handler
write -> encoded delta -> ``SnapshotCache.apply_message`` -> ``read_projection``,
the exact function the morning page renders from. Not a stand-in for any leg.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_session_replay.handlers.handler_projection_session_replay import (
    TOPIC_SESSION_STARTED,
    TOPIC_TOOL_EXECUTED,
    HandlerProjectionSessionReplay,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.morning_page import EnumPanelState, read_projection
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.snapshot_cache import SnapshotCache
from omnimarket.projection.snapshot_publisher import ModelSnapshotDeltaMessage

_TOPIC = "onex.snapshot.projection.session.replay.v1"


def _contract_path() -> Path:
    import omnimarket.nodes.node_projection_session_replay as node_pkg

    return Path(node_pkg.__file__).resolve().parent / "contract.yaml"


def _exposure() -> ProjectionTableConfig:
    """The REAL exposure, parsed from the node's own shipped contract.

    Not a hand-built ProjectionTableConfig: a test that constructs its own
    exposure proves the cache works and proves nothing about whether the
    contract this node actually ships declares the exposure that reaches it.
    """
    path = _contract_path()
    with open(path) as handle:
        contract: dict[str, Any] = yaml.safe_load(handle)
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), path
    )
    assert len(exposures) == 1, exposures
    return exposures[0]


class _RecordingPublisher:
    """Captures encoded deltas instead of reaching a broker."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.messages: list[ModelSnapshotDeltaMessage] = []

    def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        self.messages.append(message)
        return self.accept


def _dispatch(
    handler: HandlerProjectionSessionReplay,
    db: InmemoryDatabaseAdapter,
    *,
    topic: str,
    session_id: str,
    envelope_id: str | None = None,
    **payload: Any,
) -> dict[str, object]:
    """Drive the handler through the runtime's own entrypoint.

    ``handle(input_data)`` with the runtime-injected bookkeeping keys is the
    only way ``handler_wiring._invoke_projection`` calls a projection reducer,
    so the test calls exactly that rather than ``project()`` directly.
    """
    input_data: dict[str, object] = {
        "_db": db,
        "_topic": topic,
        "_event_type": "test_event",
        "session_id": session_id,
        **payload,
    }
    if envelope_id is not None:
        input_data["_envelope_id"] = envelope_id
    return handler.handle(input_data)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_declares_bus_backed_with_the_row_identity_as_its_key() -> None:
    """The exposure the runtime loads must be bus_backed, keyed on snapshot_id.

    ``snapshot_id`` is the row's own content-addressed identity. The key choice
    is load-bearing for the ordering token this handler can supply (see
    ``test_a_redelivery_reproduces_the_same_key``), so it is asserted rather
    than assumed.
    """
    exposure = _exposure()
    assert exposure.topic == _TOPIC
    assert exposure.bus_backed is True
    assert exposure.key_columns == ("snapshot_id",)
    assert exposure.tenant_column is None


# ---------------------------------------------------------------------------
# Handler -> delta
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handle_publishes_one_delta_per_upserted_row() -> None:
    publisher = _RecordingPublisher()
    handler = HandlerProjectionSessionReplay(publisher=publisher)
    db = InmemoryDatabaseAdapter()

    result = _dispatch(
        handler,
        db,
        topic=TOPIC_SESSION_STARTED,
        session_id="omn17774-chain",
        timestamp="2026-09-03T15:00:00+00:00",
    )

    assert result["rows_upserted"] == 1
    assert result["snapshot_published"] is True
    assert len(publisher.messages) == 1

    stored = db.tables["session_replay_snapshots"][0]
    message = publisher.messages[0]
    assert message.topic == _TOPIC
    assert message.key == str(stored["snapshot_id"]).encode("utf-8")
    assert message.value is not None

    delta = json.loads(message.value)
    assert delta["op"] == "upsert"
    assert delta["key"] == [stored["snapshot_id"]]
    assert delta["row"]["session_id"] == "omn17774-chain"
    assert delta["row"]["event_type"] == "session_start"
    # state_delta is declared in json_columns, so it must arrive as an object,
    # not as a JSON string the reader would have to parse a second time.
    assert delta["row"]["state_delta"] == {"session_id": "omn17774-chain"}


@pytest.mark.unit
def test_a_publish_that_did_not_land_is_reported_not_swallowed() -> None:
    """A durable row whose republish failed must say so.

    The row is in Postgres either way, so the source event must NOT be failed
    into the DLQ over an unavailable republish leg. But a silent False is the
    'consume -> ack -> nothing rendered -> no error anywhere' shape this epic
    exists to close, so the outcome rides the handler's own result.
    """
    publisher = _RecordingPublisher(accept=False)
    handler = HandlerProjectionSessionReplay(publisher=publisher)
    db = InmemoryDatabaseAdapter()

    result = _dispatch(
        handler,
        db,
        topic=TOPIC_SESSION_STARTED,
        session_id="omn17774-nobroker",
        timestamp="2026-09-03T15:00:00+00:00",
    )

    assert result["rows_upserted"] == 1
    assert result["snapshot_published"] is False


@pytest.mark.unit
def test_every_distinct_source_event_owns_its_own_key() -> None:
    """The premise the fixed ordering token depends on.

    The runtime's projection seam hands a sync handler no Kafka partition or
    offset (``handler_shim.RUNTIME_INJECTED_KEYS`` is ``_db``/``_event_type``/
    ``_topic``/``_envelope_id``), so every delta this handler publishes carries
    the same ordering coordinates. That is only safe while one source event
    owns exactly one key -- otherwise ``SnapshotCache`` would drop a genuinely
    newer row for an existing key as a replay. Assert the property directly, so
    a future change to a mutable key grain fails here rather than silently
    freezing the exposure at its first value.
    """
    publisher = _RecordingPublisher()
    handler = HandlerProjectionSessionReplay(publisher=publisher)
    db = InmemoryDatabaseAdapter()

    for index in range(3):
        _dispatch(
            handler,
            db,
            topic=TOPIC_TOOL_EXECUTED,
            session_id="omn17774-multi",
            timestamp=f"2026-09-03T15:0{index}:00+00:00",
            tool_name=f"Tool{index}",
        )

    keys = [message.key for message in publisher.messages]
    assert len(keys) == 3
    assert len(set(keys)) == 3


@pytest.mark.unit
def test_a_redelivery_reproduces_the_same_key_and_the_same_row() -> None:
    """Kafka redelivery of one event is an idempotent republish, not a new row."""
    publisher = _RecordingPublisher()
    handler = HandlerProjectionSessionReplay(publisher=publisher)
    db = InmemoryDatabaseAdapter()

    for _ in range(2):
        _dispatch(
            handler,
            db,
            topic=TOPIC_SESSION_STARTED,
            session_id="omn17774-redelivery",
            timestamp="2026-09-03T15:00:00+00:00",
            envelope_id="11111111-2222-3333-4444-555555555555",
        )

    assert len(db.tables["session_replay_snapshots"]) == 1
    assert len(publisher.messages) == 2
    first, second = publisher.messages
    assert first.key == second.key
    assert first.value is not None
    assert second.value is not None
    # observed_at is wall-clock display metadata and is allowed to differ; every
    # row field the reader renders must not.
    assert json.loads(first.value)["row"] == json.loads(second.value)["row"]


# ---------------------------------------------------------------------------
# Delta -> cache -> the function the morning page renders from
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_published_delta_makes_the_row_readable_on_the_page_path() -> None:
    """Golden chain: handler write -> published bytes -> cache -> read_projection.

    ``read_projection`` is the exact function ``build_morning_page`` calls for
    the sessions panel, and it mirrors ``GET /projection/{topic}``'s refusal
    taxonomy. Before this change it returned
    ``REFUSED / not_yet_bus_backed``; the assertion below is that the same call
    now returns the row the handler wrote.
    """
    exposure = _exposure()
    publisher = _RecordingPublisher()
    handler = HandlerProjectionSessionReplay(publisher=publisher)
    db = InmemoryDatabaseAdapter()

    _dispatch(
        handler,
        db,
        topic=TOPIC_TOOL_EXECUTED,
        session_id="omn17774-goldenchain",
        timestamp="2026-09-03T15:00:00+00:00",
        tool_name="Bash",
        tokens_used=41,
    )

    cache = SnapshotCache(
        {_TOPIC: exposure},
        bootstrap_servers="unused:9092",
        group_id="test-omn17774-session-replay",
    )
    message = publisher.messages[0]
    cache.apply_message(
        _TOPIC, key=message.key, value=message.value, headers=list(message.headers)
    )
    # Drive the real bootstrap flag, not a stub: an unbootstrapped cache
    # short-circuits to REFUSED / snapshot_bootstrap_incomplete before any row
    # is served, which is a different (and correct) refusal.
    cache._state[_TOPIC].bootstrap_complete = True

    read = read_projection(_TOPIC, {_TOPIC: exposure}, cache, limit=50)

    assert read.state is EnumPanelState.LIVE, (read.reason_code, read.reason_detail)
    assert read.cached_row_count == 1
    assert len(read.rows) == 1
    row = read.rows[0]
    assert row["session_id"] == "omn17774-goldenchain"
    assert row["event_type"] == "tool_call"
    assert row["node_name"] == "Bash"
    assert row["cumulative_tokens"] == 41
