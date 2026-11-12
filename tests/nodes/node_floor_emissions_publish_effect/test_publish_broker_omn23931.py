# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor emissions projection (OMN-23931): the snapshot may be published on a broker other than the one the emission topics are read from."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.floor_projection import (
    ModelFloorEmissionsOverlay,
)
from omnimarket.nodes.node_floor_emissions_publish_effect.runtime_floor_emissions_publish import (
    build_publisher,
    build_source,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_emissions_publish_effect"
)
OVERLAY = ModelFloorEmissionsOverlay.model_validate(
    yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
)
OPEN_LANE_BUS = (
    "omnimarket.nodes.node_floor_emissions_publish_effect"
    ".runtime_floor_emissions_publish.open_lane_bus"
)


class BusOpenerRecorder:
    """Stands in for open_lane_bus: records the calls, each answer an async context manager stub."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        bus: str,
        lane: str | None,
        kafka_bootstrap: str | None,
        root: Any,
    ) -> Any:
        self.calls.append(
            {"bus": bus, "lane": lane, "kafka_bootstrap": kafka_bootstrap, "root": root}
        )

        @contextlib.asynccontextmanager
        async def stub() -> AsyncIterator[Any]:
            yield None

        return stub()


def kafka_overlay(
    snapshot_kafka_bootstrap: str | None, bus_lane: str | None = "dev"
) -> ModelFloorEmissionsOverlay:
    return OVERLAY.model_copy(
        update={
            "bus": "kafka",
            "bus_lane": bus_lane,
            "snapshot_kafka_bootstrap": snapshot_kafka_bootstrap,
        }
    )


def _entered(publisher: Any, source: Any) -> None:
    async def enter() -> None:
        async with publisher._open_bus():
            pass
        async with source._open_bus():
            pass

    asyncio.run(enter())


def test_golden_publish_goes_to_the_named_snapshot_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = BusOpenerRecorder()
    monkeypatch.setattr(OPEN_LANE_BUS, recorder)
    overlay = kafka_overlay("snapshot-broker:9092")
    publisher = build_publisher(overlay, {})
    source = build_source(overlay, {}, "g")
    _entered(publisher, source)
    assert [(c["kafka_bootstrap"], c["lane"]) for c in recorder.calls] == [
        ("snapshot-broker:9092", None),
        (None, "dev"),
    ]


def test_error_chain_overlay_refuses_a_blank_unknown_key_shape() -> None:
    raw = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    with pytest.raises(ValidationError, match="Extra inputs"):
        ModelFloorEmissionsOverlay.model_validate({**raw, "snapshot_bootstrap": "x"})


def test_control_no_snapshot_broker_publishes_on_the_read_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = BusOpenerRecorder()
    monkeypatch.setattr(OPEN_LANE_BUS, recorder)
    overlay = kafka_overlay(None)
    publisher = build_publisher(overlay, {})
    source = build_source(overlay, {}, "g")
    _entered(publisher, source)
    assert [(c["kafka_bootstrap"], c["lane"]) for c in recorder.calls] == [
        (None, "dev"),
        (None, "dev"),
    ]


def test_overlay_without_the_key_still_validates() -> None:
    raw = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    del raw["snapshot_kafka_bootstrap"]
    overlay = ModelFloorEmissionsOverlay.model_validate(raw)
    assert overlay.snapshot_kafka_bootstrap is None
