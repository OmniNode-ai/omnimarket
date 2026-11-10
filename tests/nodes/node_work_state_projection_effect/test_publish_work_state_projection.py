# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-state projection as the ``work_state`` floor projection (OMN-23391): golden chain, error chains, the bound."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope
from omnimarket.models.work_state_projection import ModelWorkStateResult
from omnimarket.nodes.node_work_state_projection_effect import runtime_work_state as rw
from tests.nodes.node_floor_simple_effect.test_work_state_lanes_floor_simple import (
    ROWS,
    project,
)
from tests.unit.nodes.node_overlay_store.support import FakeBus

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 22, 0, tzinfo=UTC)
TOPIC = "onex.snapshot.projection.floor-work-state.v1"


def _result() -> ModelWorkStateResult:
    return project(*ROWS)


def _overlay(tmp_path: Path, **over: Any) -> dict[str, str]:
    path = tmp_path / "publish.yaml"
    path.write_text(
        yaml.safe_dump(
            {"bus": "kafka", "bus_lane": "test-bus", "projection_id": "work_state"}
            | over
        ),
        encoding="utf-8",
    )
    return {"ONEX_FLOOR_WORK_STATE_OVERLAY": str(path)}


def _use(monkeypatch: pytest.MonkeyPatch, bus: FakeBus) -> None:
    from omnimarket.handlers.floor_projection import envelope_publisher

    @contextlib.asynccontextmanager
    async def opened(**_: Any) -> AsyncIterator[FakeBus]:
        yield bus

    monkeypatch.setattr(envelope_publisher, "open_lane_bus", opened)


def test_golden_chain_the_projection_is_published_stamped_with_the_time_it_was_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = FakeBus()
    _use(monkeypatch, bus)
    assert rw.publish_projection(_result(), _overlay(tmp_path), NOW) == "published"
    ((topic, key, value),) = bus.published
    assert topic == TOPIC
    assert key == b"work_state"
    envelope = ModelFloorProjectionEnvelope.model_validate_json(value)
    assert envelope.as_of == NOW
    assert ModelWorkStateResult.model_validate(envelope.payload) == _result()


def test_error_chain_no_publish_overlay_publishes_nothing(tmp_path: Path) -> None:
    assert rw.publish_projection(_result(), {}, NOW) == "not configured"


def test_the_in_memory_default_holds_the_publication_nowhere(tmp_path: Path) -> None:
    env = _overlay(tmp_path, bus="inmemory", bus_lane=None)
    assert rw.publish_projection(_result(), env, NOW) == "not published"


def test_named_break_a_projection_over_the_bound_is_refused_with_its_size_not_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = FakeBus()
    _use(monkeypatch, bus)
    monkeypatch.setattr(
        rw,
        "_runtime",
        lambda: {
            **rw.yaml.safe_load(rw._CONTRACT.read_text(encoding="utf-8"))["runtime"],
            "publish": {
                **rw.yaml.safe_load(rw._CONTRACT.read_text(encoding="utf-8"))[
                    "runtime"
                ]["publish"],
                "max_payload_bytes": 10,
            },
        },
    )
    with pytest.raises(ValueError, match="over the 10 byte bound"):
        rw.publish_projection(_result(), _overlay(tmp_path), NOW)
    assert bus.published == []
