# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled work-state projection publishes the work_state floor projection from the runtime (OMN-25054).

The publish used to run only from the node's own CLI, so the runtime's scheduled tick wrote the file and the event but
never the bus projection the floor reads. The tick now publishes after its run; a bus that refuses leaves the run and its
event intact.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope
from omnimarket.models.work_state_projection import ModelWorkStateResult
from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    HandlerWorkStateProjectionScheduled,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    RULES_EXAMPLE,
    T0,
    schedule,
    tick,
)
from tests.nodes.node_work_state_projection_scheduled_effect.test_state_home import (
    EFFECT,
    _contract,
)
from tests.unit.nodes.node_overlay_store.support import FakeBus

pytestmark = pytest.mark.unit

TOPIC = "onex.snapshot.projection.floor-work-state.v1"


def _env(tmp_path: Path, publish: bool) -> dict[str, str]:
    home = tmp_path / "home"
    env = {"OMNI_HOME": str(home), "ONEX_WORK_STATE_OVERLAY": str(RULES_EXAMPLE)}
    # One source the run can read, at the path the effect node's contract names (as test_state_home seeds it).
    watcher = home / _contract(EFFECT)["runtime"]["watcher"]["relpath"]
    watcher.parent.mkdir(parents=True)
    watcher.write_text(json.dumps({"prs": {}}), encoding="utf-8")
    if publish:
        overlay = tmp_path / "publish.yaml"
        overlay.write_text(
            yaml.safe_dump(
                {"bus": "kafka", "bus_lane": "test-bus", "projection_id": "work_state"}
            ),
            encoding="utf-8",
        )
        env["ONEX_FLOOR_WORK_STATE_OVERLAY"] = str(overlay)
    return env


def _use(monkeypatch: pytest.MonkeyPatch, bus: Any) -> None:
    from omnimarket.handlers.floor_projection import envelope_publisher

    @contextlib.asynccontextmanager
    async def opened(**_: Any) -> AsyncIterator[Any]:
        yield bus

    monkeypatch.setattr(envelope_publisher, "open_lane_bus", opened)


def test_golden_the_scheduled_tick_publishes_the_projection_it_just_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = FakeBus()
    _use(monkeypatch, bus)
    handler = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="file_sources"), environ=_env(tmp_path, True)
    )
    assert handler.handle(tick(T0)) is not None
    ((topic, key, value),) = bus.published
    assert topic == TOPIC
    assert key == b"work_state"
    envelope = ModelFloorProjectionEnvelope.model_validate_json(value)
    assert envelope.producer == "node_work_state_projection_effect"
    assert ModelWorkStateResult.model_validate(envelope.payload).as_of == T0


def test_error_chain_a_bus_that_refuses_leaves_the_run_and_its_event_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class RefusingBus:
        async def publish(self, *_: Any) -> None:
            raise ConnectionError("broker refused the login")

    _use(monkeypatch, RefusingBus())
    handler = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="file_sources"), environ=_env(tmp_path, True)
    )
    with caplog.at_level(logging.WARNING):
        assert handler.handle(tick(T0)) is not None
    assert "not published (ConnectionError: broker refused the login)" in caplog.text


def test_no_publish_overlay_publishes_nothing_and_the_run_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = FakeBus()
    _use(monkeypatch, bus)
    handler = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="file_sources"), environ=_env(tmp_path, False)
    )
    assert handler.handle(tick(T0)) is not None
    assert bus.published == []
