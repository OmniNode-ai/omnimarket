# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The integration readings as a floor projection (OMN-23899 AC2): golden chain on the in-memory bus, error chains.

The golden chain runs the contract's publisher on the in-memory event bus and the floor's own projection reader on the
same bus: what the pass handed over is what the floor's bundle holds, with the pass's time. The error chains show an
invalid batch and an unreadable overlay publishing nothing, each ending its chain in a typed failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.container.container_inmemory import bind_infra_event_bus
from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.handlers.floor_projection import (
    BusSnapshotSource,
    load_bundle,
    projection_payload,
)
from omnimarket.models.floor_projection import (
    ModelFloorProjectionPublishOverlay,
    ModelFloorProjectionReadOverlay,
)
from omnimarket.models.integration_reading import (
    ModelIntegrationProjection,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.handlers import (
    InMemoryEnvelopePublisher,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.runtime_floor_owner_map_publish import (
    BusEnvelopePublisher,
)
from omnimarket.nodes.node_floor_projection_read_effect.runtime_floor_projection_read import (
    run as read_projections,
)
from omnimarket.nodes.node_integration_reading_publish_effect import (
    runtime_integration_reading_publish as runtime,
)
from tests.nodes.node_integration_projection_compute.test_integration_projection import (
    BATCH,
    GOLDEN_COUNTS,
)

pytestmark = pytest.mark.unit

NODE = "node_integration_reading_publish_effect"
READ_NODE = "node_floor_projection_read_effect"
AS_OF = datetime(2026, 11, 12, 1, 25, tzinfo=UTC)
OVERLAY = ModelFloorProjectionPublishOverlay(
    bus="inmemory", projection_id="integration"
)


def _contract(node: str) -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{node}")
        .joinpath("contract.yaml")
        .read_text()
    )
    return dict(yaml.safe_load(text))


def _example(node: str) -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{node}")
        .joinpath("overlay.example.yaml")
        .read_text()
    )
    return dict(yaml.safe_load(text))


TOPIC = _contract(NODE)["runtime"]["topic"]


def _batch() -> ModelIntegrationReadingBatch:
    return ModelIntegrationReadingBatch.model_validate(BATCH)


def test_seam_the_publisher_the_reader_and_the_overlays_agree_on_topic_and_id() -> None:
    publisher = _contract(NODE)
    reader = _contract(READ_NODE)
    assert TOPIC in publisher["event_bus"]["publish_topics"]
    assert TOPIC in reader["snapshot_reads"]
    published_as = ModelFloorProjectionPublishOverlay.model_validate(
        _example(NODE)
    ).projection_id
    reader_overlay = ModelFloorProjectionReadOverlay.model_validate(_example(READ_NODE))
    assert reader_overlay.projections[published_as].topic == TOPIC


@pytest.mark.asyncio
async def test_golden_chain_the_batch_reaches_the_floor_bundle_over_the_in_memory_bus(
    tmp_path: Path,
) -> None:
    bus = bind_infra_event_bus(environment="local", group="integration-seam")
    await bus.start()

    @contextlib.asynccontextmanager
    async def open_bus() -> AsyncIterator[Any]:
        yield bus

    sink = InMemoryEmissionSink()
    reader = BusSnapshotSource(
        open_bus=open_bus, group_prefix="integration-seam", window_s=2.0
    )
    listen = asyncio.create_task(reader.publications(TOPIC))
    await asyncio.sleep(0.3)
    result = await runtime.run(
        _batch(),
        OVERLAY,
        BusEnvelopePublisher(open_bus, TOPIC),
        sink,
        producer="pass",
        clock=lambda: AS_OF,
    )
    publications = await listen
    assert result.published
    assert result.entries == 5
    assert result.as_of == AS_OF
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert sink.events[1].counters == {"rows_published": 5, "unintegrated": 3}
    assert len(publications) == 1

    # The floor's own reader, over the publications the bus delivered, writes the bundle the floor cycle reads.
    held: dict[str, list[bytes]] = {TOPIC: list(publications)}
    from omnimarket.handlers.floor_projection import InMemorySnapshotSource

    read_overlay = ModelFloorProjectionReadOverlay.model_validate(
        {
            "bus": "inmemory",
            "read_window_s": 0.1,
            "projections": {"integration": {"topic": TOPIC, "max_age_s": 7800}},
        }
    )
    bundle_result = await read_projections(
        tmp_path / "home",
        read_overlay,
        InMemorySnapshotSource(held),
        InMemoryEmissionSink(),
        clock=lambda: AS_OF + timedelta(minutes=5),
    )
    bundle = load_bundle(Path(bundle_result.output_path))
    fresh = projection_payload(bundle, "integration", AS_OF + timedelta(minutes=10))
    assert fresh.payload is not None
    assert fresh.as_of == AS_OF
    projection = ModelIntegrationProjection.model_validate(fresh.payload)
    assert projection.counts.model_dump(mode="json") == GOLDEN_COUNTS
    # The control: three hours on with no new pass, the same projection reads unknown, never as the old list.
    stale = projection_payload(bundle, "integration", AS_OF + timedelta(hours=3))
    assert stale.payload is None
    assert "over its 7800s bound" in stale.reason


def test_named_break_with_no_publish_the_floor_holds_no_integration_projection(
    tmp_path: Path,
) -> None:
    """The failing control for the golden chain: the same pass with its publish removed leaves the floor unread."""
    from omnimarket.handlers.floor_projection import InMemorySnapshotSource

    read_overlay = ModelFloorProjectionReadOverlay.model_validate(
        {
            "bus": "inmemory",
            "read_window_s": 0.1,
            "projections": {"integration": {"topic": TOPIC, "max_age_s": 7800}},
        }
    )
    bundle_result = asyncio.run(
        read_projections(
            tmp_path / "home",
            read_overlay,
            InMemorySnapshotSource({}),
            InMemoryEmissionSink(),
            clock=lambda: AS_OF,
        )
    )
    entry = projection_payload(
        load_bundle(Path(bundle_result.output_path)), "integration", AS_OF
    )
    assert entry.payload is None
    assert "holds no valid publication of integration" in entry.reason


def test_error_chain_a_bus_that_refuses_fails_the_chain_and_raises() -> None:
    class Refusing:
        async def publish(self, envelope: Any) -> None:
            raise ConnectionError("refused")

    sink = InMemoryEmissionSink()
    with pytest.raises(ConnectionError):
        asyncio.run(runtime.run(_batch(), OVERLAY, Refusing(), sink, producer="pass"))
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[-1].failure_code == "integration_publish_failed"


def _main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdin: str, overlay: str | None
) -> tuple[int, list[dict[str, Any]], list[Any]]:
    published: list[Any] = []
    memory = InMemoryEnvelopePublisher()
    monkeypatch.setattr(runtime, "build_publisher", lambda *_args: memory)
    if overlay is not None:
        path = tmp_path / "overlay.yaml"
        path.write_text(overlay, encoding="utf-8")
        monkeypatch.setenv("ONEX_FLOOR_INTEGRATION_OVERLAY", str(path))
    else:
        monkeypatch.setenv(
            "ONEX_FLOOR_INTEGRATION_OVERLAY", str(tmp_path / "absent.yaml")
        )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-roots"))
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = runtime.main(["--home", str(tmp_path / "home")])
    journal = tmp_path / "home" / _contract(NODE)["runtime"]["journal_relpath"]
    events = (
        [json.loads(line) for line in journal.read_text().splitlines()]
        if journal.exists()
        else []
    )
    published.extend(memory.published)
    return code, events, published


EXAMPLE = (
    resources.files(f"omnimarket.nodes.{NODE}")
    .joinpath("overlay.example.yaml")
    .read_text()
)


def test_golden_chain_the_cli_takes_the_batch_on_stdin_and_publishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, events, published = _main(monkeypatch, tmp_path, json.dumps(BATCH), EXAMPLE)
    assert code == 0
    assert [e["phase"] for e in events] == ["start", "success"]
    (envelope,) = published
    assert envelope.projection_id == "integration"
    assert envelope.payload["counts"] == GOLDEN_COUNTS


@pytest.mark.parametrize(
    "stdin",
    [
        "not json",
        json.dumps(
            {**BATCH, "readings": [{**BATCH["readings"][0], "gap_class": "none"}]}
        ),
        "",
    ],
    ids=["not-json", "rule-broken", "empty"],
)
def test_error_chain_an_invalid_batch_exits_65_publishes_nothing_and_fails_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdin: str
) -> None:
    code, events, published = _main(monkeypatch, tmp_path, stdin, EXAMPLE)
    assert code == 65
    assert published == []
    assert [e["phase"] for e in events] == ["start", "failure"]
    assert events[-1]["failure_code"] == "integration_batch_invalid"


def test_error_chain_an_unreadable_overlay_exits_65_before_reading_the_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, events, published = _main(monkeypatch, tmp_path, json.dumps(BATCH), None)
    assert code == 65
    assert published == []
    assert events[-1]["failure_code"] == "integration_overlay_unreadable"
