# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's projection reader (OMN-23391): golden chain, error chain, and the named breaks.

A projection carries its producer's own time. The reader keeps it, the floor judges it against the projection's own bound, and
a projection older than that bound reads unknown, whatever the file or the bundle that carried it says about when it was written.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.container.container_inmemory import bind_infra_event_bus
from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.handlers.floor_projection import (
    BusSnapshotSource,
    InMemorySnapshotSource,
    load_bundle,
    projection_payload,
    publish_envelope,
)
from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionReadOverlay,
)
from omnimarket.nodes.node_floor_projection_read_effect.runtime_floor_projection_read import (
    run,
)

NOW = datetime(2026, 11, 9, 22, 0, tzinfo=UTC)
TOPIC = "onex.snapshot.projection.floor-overlay-store-fleet.v1"
OVERLAY = ModelFloorProjectionReadOverlay.model_validate(
    {
        "bus": "inmemory",
        "read_window_s": 0.2,
        "projections": {"overlay_store_fleet": {"topic": TOPIC, "max_age_s": 900}},
    }
)


def _envelope(
    as_of: datetime, readings: dict[str, Any] | None = None, producer: str = "h1"
) -> bytes:
    return (
        ModelFloorProjectionEnvelope(
            projection_id="overlay_store_fleet",
            as_of=as_of,
            producer=producer,
            payload={"readings": readings or {"h1:lab_hosts": {"state": "fresh"}}},
        )
        .model_dump_json()
        .encode()
    )


def _run(tmp_path: Path, source: Any, sink: InMemoryEmissionSink) -> Any:
    return asyncio.run(run(tmp_path, OVERLAY, source, sink, clock=lambda: NOW))


def test_golden_chain_a_fresh_publication_is_written_with_its_own_time(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    as_of = NOW - timedelta(seconds=30)
    result = _run(tmp_path, InMemorySnapshotSource({TOPIC: [_envelope(as_of)]}), sink)
    assert (result.read, result.unread) == (1, 0)
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert sink.events[1].counters == {"projections_read": 1}
    bundle = load_bundle(Path(result.output_path))
    read = projection_payload(bundle, "overlay_store_fleet", NOW)
    assert read.payload == {"readings": {"h1:lab_hosts": {"state": "fresh"}}}
    assert read.as_of == as_of
    assert read.age_s == 30
    assert read.producer == "h1"


def test_the_newest_publication_by_its_own_time_wins_whatever_the_order(
    tmp_path: Path,
) -> None:
    newer = NOW - timedelta(seconds=10)
    older = NOW - timedelta(seconds=500)
    source = InMemorySnapshotSource(
        {TOPIC: [_envelope(newer, producer="h2"), _envelope(older, producer="h1")]}
    )
    result = _run(tmp_path, source, InMemoryEmissionSink())
    read = projection_payload(
        load_bundle(Path(result.output_path)), "overlay_store_fleet", NOW
    )
    assert read.producer == "h2"
    assert read.age_s == 10


def test_error_chain_no_publication_is_unread_with_a_typed_failure(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    result = _run(tmp_path, InMemorySnapshotSource(), sink)
    assert (result.read, result.unread) == (0, 1)
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == "projection_unread"
    assert "overlay_store_fleet" in sink.events[1].failure_detail
    read = projection_payload(
        load_bundle(Path(result.output_path)), "overlay_store_fleet", NOW
    )
    assert read.payload is None
    assert "holds no valid publication" in read.reason


def test_error_chain_a_broker_that_cannot_be_read_is_unread_not_a_crash(
    tmp_path: Path,
) -> None:
    class Down:
        async def publications(self, topic: str) -> list[bytes]:
            raise ConnectionError("no broker")

    sink = InMemoryEmissionSink()
    result = _run(tmp_path, Down(), sink)
    assert result.unread == 1
    assert sink.events[1].failure_code == "projection_unread"
    read = projection_payload(
        load_bundle(Path(result.output_path)), "overlay_store_fleet", NOW
    )
    assert read.payload is None
    assert "ConnectionError" in read.reason


def test_a_publication_that_does_not_parse_or_names_another_projection_is_skipped(
    tmp_path: Path,
) -> None:
    other = (
        ModelFloorProjectionEnvelope(
            projection_id="somebody_else", as_of=NOW, producer="h9", payload={"x": 1}
        )
        .model_dump_json()
        .encode()
    )
    source = InMemorySnapshotSource(
        {TOPIC: [b"not json", other, b'{"projection_id": "overlay_store_fleet"}']}
    )
    result = _run(tmp_path, source, InMemoryEmissionSink())
    assert result.unread == 1
    read = projection_payload(
        load_bundle(Path(result.output_path)), "overlay_store_fleet", NOW
    )
    assert "3 skipped" in read.reason


def test_a_bare_time_cannot_be_aged_so_it_is_refused() -> None:
    with pytest.raises(ValueError, match="time zone"):
        ModelFloorProjectionEnvelope(
            projection_id="overlay_store_fleet",
            as_of=datetime(2026, 11, 9, 22, 0),
            producer="h1",
            payload={},
        )


def test_named_break_a_projection_older_than_its_bound_reads_unknown_though_the_bundle_is_new(
    tmp_path: Path,
) -> None:
    """The producer stopped 2 hours ago; the reader keeps running and writes a new bundle every pass."""
    stale = NOW - timedelta(hours=2)
    result = _run(
        tmp_path,
        InMemorySnapshotSource({TOPIC: [_envelope(stale)]}),
        InMemoryEmissionSink(),
    )
    bundle = load_bundle(Path(result.output_path))
    assert bundle is not None
    assert bundle.read_at == NOW
    read = projection_payload(bundle, "overlay_store_fleet", NOW)
    assert read.payload is None
    assert read.reason == "the projection is 7200s old, over its 900s bound"
    # The control: judged only by the bundle's own write time, as a file's mtime would be, the same data reads fresh.
    assert (NOW - bundle.read_at).total_seconds() == 0


def test_a_missing_or_foreign_bundle_reads_unknown_not_empty(tmp_path: Path) -> None:
    assert load_bundle(tmp_path / "absent.json") is None
    read = projection_payload(None, "overlay_store_fleet", NOW)
    assert read.payload is None
    assert "were not written" in read.reason
    (tmp_path / "bundle.json").write_text("{}", encoding="utf-8")
    assert load_bundle(tmp_path / "bundle.json") is None


@pytest.mark.asyncio
async def test_golden_chain_over_a_bus_the_producer_publishes_and_the_reader_listens() -> (
    None
):
    """The producer's side and the reader's side over one bus, the way the broker backend runs them."""
    bus = bind_infra_event_bus(environment="local", group="floor-projection-golden")
    await bus.start()

    @contextlib.asynccontextmanager
    async def open_bus() -> AsyncIterator[Any]:
        yield bus

    reader = BusSnapshotSource(open_bus=open_bus, group_prefix="floor", window_s=0.2)
    listen = asyncio.create_task(reader.publications(TOPIC))
    await asyncio.sleep(0.05)
    await publish_envelope(
        bus,
        TOPIC,
        ModelFloorProjectionEnvelope(
            projection_id="overlay_store_fleet",
            as_of=NOW,
            producer="h1",
            payload={"readings": {}},
        ),
    )
    publications = await listen
    assert len(publications) == 1
    assert (
        ModelFloorProjectionEnvelope.model_validate_json(publications[0]).projection_id
        == "overlay_store_fleet"
    )
