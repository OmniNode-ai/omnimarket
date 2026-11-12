# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor emissions seam (OMN-23931): one projection id and one snapshot topic, named by the publisher, declared by the reader and read by the floor tick.

A change of shape on either side of the seam fails here: the id the publisher publishes under is the id the floor tick reads, the
snapshot topic it publishes on is the one its contract declares published and externally consumed, and the reader's overlay accepts an
entry for that topic. The golden chain is folded into the envelope the way the publisher publishes it and read back the way the floor
reads it: the bundle as written, the projection judged against its own bound, the payload decoded to the events it holds. A projection
older than its bound holds no chain.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.handlers.floor_projection import (
    InMemorySnapshotSource,
    emissions_from_payload,
    load_bundle,
    projection_payload,
)
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.models.floor_17a.model_floor_17a import ChainOrigin
from omnimarket.models.floor_projection import (
    ModelFloorEmissionsOverlay,
    ModelFloorProjectionBundle,
    ModelFloorProjectionEntry,
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionReadOverlay,
    ModelFloorProjectionSpec,
)
from omnimarket.nodes.node_floor_emissions_publish_effect.handlers import (
    HandlerFloorEmissionsPublish,
    InMemoryEnvelopePublisher,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src/omnimarket/nodes/node_floor_emissions_publish_effect"
READER_DIR = ROOT / "src/omnimarket/nodes/node_floor_projection_read_effect"
TICK_DIR = ROOT / "src/omnimarket/nodes/node_floor_simple_effect"
MAX_AGE_S = 900
OVERLAY = ModelFloorEmissionsOverlay.model_validate(
    yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
)
STARTED, SUCCEEDED, FAILED = OVERLAY.emission_topics


def contract(directory: Path) -> dict[str, Any]:
    return yaml.safe_load((directory / "contract.yaml").read_text())


PUBLISHER_CONTRACT = contract(NODE_DIR)
TICK_CONTRACT = contract(TICK_DIR)


def chain() -> list[ModelNodeEmission]:
    sink = InMemoryEmissionSink()
    emitter = ChainEmitter(
        sink,
        node="node_x:x",
        piece=3,
        clock=lambda: NOW,
        correlation_id="seam-1",
        origin=cast(ChainOrigin, "real"),
    )
    emitter.start()
    emitter.success({"n": 1})
    return sink.events


def topic_of(event: ModelNodeEmission) -> str:
    return {"start": STARTED, "success": SUCCEEDED, "failure": FAILED}[event.phase]


def held(events: list[ModelNodeEmission]) -> InMemorySnapshotSource:
    by_topic: dict[str, list[bytes]] = {t: [] for t in OVERLAY.emission_topics}
    for e in events:
        by_topic[topic_of(e)].append(e.model_dump_json().encode())
    return InMemorySnapshotSource(by_topic)


def published_envelope() -> ModelFloorProjectionEnvelope:
    """One real chain folded into the envelope the publisher would put on the snapshot topic."""
    publisher = InMemoryEnvelopePublisher()
    result = asyncio.run(
        HandlerFloorEmissionsPublish(
            source=held(chain()),
            publisher=publisher,
            overlay=OVERLAY,
            producer="seam:host",
            clock=lambda: NOW,
        ).handle()
    )
    assert result.published
    (envelope,) = publisher.published
    return envelope


def bundle_of(envelope: ModelFloorProjectionEnvelope) -> ModelFloorProjectionBundle:
    """The bundle the reader writes for this envelope: one entry, read, bounded at 900 s."""
    return ModelFloorProjectionBundle(
        read_at=envelope.as_of,
        entries={
            envelope.projection_id: ModelFloorProjectionEntry(
                projection_id=envelope.projection_id,
                status="read",
                max_age_s=MAX_AGE_S,
                read_at=envelope.as_of,
                as_of=envelope.as_of,
                producer=envelope.producer,
                payload=envelope.payload,
            )
        },
    )


def test_publisher_projection_id_is_the_one_the_floor_tick_reads() -> None:
    assert (
        OVERLAY.projection_id
        == TICK_CONTRACT["runtime"]["sources"]["emissions_projection"]
    )


def test_publisher_snapshot_topic_is_published_by_the_contract_and_declared_externally_consumed() -> (
    None
):
    assert OVERLAY.snapshot_topic in PUBLISHER_CONTRACT["event_bus"]["publish_topics"]
    assert OVERLAY.snapshot_topic in PUBLISHER_CONTRACT["externally_consumed_topics"]


def test_a_reader_overlay_entry_for_the_publisher_topic_is_valid() -> None:
    raw = yaml.safe_load((READER_DIR / "overlay.example.yaml").read_text())
    raw["projections"][OVERLAY.projection_id] = ModelFloorProjectionSpec(
        topic=OVERLAY.snapshot_topic, max_age_s=MAX_AGE_S
    ).model_dump()
    reader = ModelFloorProjectionReadOverlay.model_validate(raw)
    assert reader.projections[OVERLAY.projection_id].topic == OVERLAY.snapshot_topic
    assert reader.projections[OVERLAY.projection_id].max_age_s == MAX_AGE_S
    assert (
        OVERLAY.projection_id
        == TICK_CONTRACT["runtime"]["sources"]["emissions_projection"]
    )


def test_golden_chain_a_published_real_chain_reads_real_through_the_reader_bundle(
    tmp_path: Path,
) -> None:
    envelope = published_envelope()
    path = tmp_path / "projection-reads.json"
    path.write_text(bundle_of(envelope).model_dump_json())
    read = load_bundle(path)
    assert read is not None
    fresh = projection_payload(
        read, envelope.projection_id, envelope.as_of + timedelta(seconds=5)
    )
    assert fresh.payload is not None
    assert fresh.reason == ""
    assert [e.origin for e in emissions_from_payload(fresh.payload)] == [
        "real",
        "real",
    ]


def test_error_chain_a_projection_older_than_its_bound_holds_no_chain(
    tmp_path: Path,
) -> None:
    envelope = published_envelope()
    path = tmp_path / "projection-reads.json"
    path.write_text(bundle_of(envelope).model_dump_json())
    read = load_bundle(path)
    assert read is not None
    late = projection_payload(
        read,
        envelope.projection_id,
        envelope.as_of + timedelta(seconds=MAX_AGE_S + 60),
    )
    assert late.payload is None
    assert late.reason
    assert emissions_from_payload(late.payload or {}) == ()
