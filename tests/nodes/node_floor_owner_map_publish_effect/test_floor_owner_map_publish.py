# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The owner map as a floor projection (OMN-23391): golden chain, error chains and the named breaks."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.handlers.floor_projection import (
    InMemorySnapshotSource,
    load_bundle,
    owners_from_payload,
    projection_payload,
)
from omnimarket.models.floor_projection import (
    ModelFloorProjectionPublishOverlay,
    ModelFloorProjectionReadOverlay,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.handlers import (
    InMemoryEnvelopePublisher,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.runtime_floor_owner_map_publish import (
    run,
)
from omnimarket.nodes.node_floor_projection_read_effect.runtime_floor_projection_read import (
    run as read_projections,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 22, 0, tzinfo=UTC)
TOPIC = "onex.snapshot.projection.floor-owner-map.v1"
OVERLAY = ModelFloorProjectionPublishOverlay(bus="inmemory", projection_id="owner_map")
STATE: dict[str, Any] = {
    "leases": [
        {"pr": "OmniNode-ai/omnimarket#1", "lease_id": 7},
        {"pr": "cause-key", "lease_id": 9},
    ],
    "causes": [
        {"key": "cause-key", "members": [{"pr": "OmniNode-ai/omnimarket#2"}]},
        {
            "key": "parked-key",
            "parked_until": "2026-11-10T00:00:00Z",
            "members": [{"pr": "OmniNode-ai/omnimarket#3"}],
        },
    ],
}


def _write(tmp_path: Path, state: Any = STATE) -> Path:
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


def _run(source: Path, publisher: InMemoryEnvelopePublisher, sink: Any) -> Any:
    return asyncio.run(
        run(source, OVERLAY, publisher, sink, producer="ctl", clock=lambda: NOW)
    )


def test_golden_chain_the_controller_state_is_published_as_the_owner_map_with_its_time(
    tmp_path: Path,
) -> None:
    publisher, sink = InMemoryEnvelopePublisher(), InMemoryEmissionSink()
    result = _run(_write(tmp_path), publisher, sink)
    assert result.published
    assert result.entries == 3
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert sink.events[1].counters == {"owners_published": 3}
    (envelope,) = publisher.published
    assert envelope.projection_id == "owner_map"
    assert envelope.as_of == NOW
    owners = owners_from_payload(envelope.payload)
    assert owners is not None
    assert owners.of("omnimarket#1") == ("landing-L7", "")
    assert owners.of("omnimarket#2") == ("", "cause-L9 cause-key")
    assert owners.of("omnimarket#3")[1].startswith("parked parked-key until")


@pytest.mark.parametrize("state", ["not json", None], ids=["not a state", "absent"])
def test_error_chain_a_state_that_cannot_be_read_publishes_nothing_and_fails_typed(
    tmp_path: Path, state: str | None
) -> None:
    if state is not None:
        (tmp_path / "state.json").write_text(state, encoding="utf-8")
    publisher, sink = InMemoryEnvelopePublisher(), InMemoryEmissionSink()
    result = _run(tmp_path, publisher, sink)
    assert not result.published
    assert publisher.published == []
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == "controller_state_unreadable"


def test_error_chain_a_bus_that_refuses_fails_the_chain_and_raises(
    tmp_path: Path,
) -> None:
    class Refusing:
        async def publish(self, envelope: Any) -> None:
            raise ConnectionError("refused")

    sink = InMemoryEmissionSink()
    with pytest.raises(ConnectionError):
        asyncio.run(
            run(
                _write(tmp_path),
                OVERLAY,
                Refusing(),
                sink,
                producer="ctl",
                clock=lambda: NOW,
            )
        )
    assert sink.events[-1].failure_code == "owner_map_publish_failed"


def test_a_payload_that_is_not_a_map_is_none() -> None:
    assert owners_from_payload(None) is None
    assert owners_from_payload({"landing": {"a": 1}, "cause": {}, "parked": {}}) is None
    assert owners_from_payload({"landing": {}}) is None


def test_named_break_the_owner_column_fills_from_the_projection_with_the_local_file_deleted(
    tmp_path: Path,
) -> None:
    """Publish, delete the controller's state file, read the projection back through the reader: the owners are still there."""
    publisher = InMemoryEnvelopePublisher()
    (tmp_path / "ctl").mkdir()
    source_dir = _write(tmp_path / "ctl")
    _run(source_dir, publisher, InMemoryEmissionSink())
    (source_dir / "state.json").unlink()
    overlay = ModelFloorProjectionReadOverlay.model_validate(
        {
            "bus": "inmemory",
            "read_window_s": 0.1,
            "projections": {"owner_map": {"topic": TOPIC, "max_age_s": 900}},
        }
    )
    held = InMemorySnapshotSource(
        {TOPIC: [publisher.published[0].model_dump_json().encode()]}
    )
    result = asyncio.run(
        read_projections(
            tmp_path / "home", overlay, held, InMemoryEmissionSink(), clock=lambda: NOW
        )
    )
    bundle = load_bundle(Path(result.output_path))
    fresh = projection_payload(bundle, "owner_map", NOW + timedelta(seconds=60))
    owners = owners_from_payload(fresh.payload)
    assert owners is not None
    assert owners.of("omnimarket#1") == ("landing-L7", "")
    # And the control: three hours on, with the controller's host silent, the same projection reads unknown.
    stale = projection_payload(bundle, "owner_map", NOW + timedelta(hours=3))
    assert stale.payload is None
    assert owners_from_payload(stale.payload) is None
