# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled work-state projection publishes the projection on the bus, stamped with its own time (OMN-23427).

Golden: a due tick in the file_sources profile writes the file and publishes one envelope on the work-state snapshot topic.
Failing control: a projection read after its bound reads unknown on the floor's side. Error chains: a bus that refuses, no publish
overlay, and a publication made from inside a running event loop (the runtime calls a handler from one).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.floor_projection import projection_payload
from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope
from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    HandlerWorkStateProjectionScheduled,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.protocols import (
    adapter_file_sources_projector as adapter,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    schedule,
    tick,
)
from tests.nodes.node_work_state_projection_scheduled_effect.test_seam_projection_file_floor_and_fill import (
    READ_OVERLAY,
    TOPIC,
    _contract,
    _floor_bundle,
    _home,
    _publish,
)

pytestmark = pytest.mark.unit


def _run_scheduled(env: dict[str, str], now):
    handler = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="file_sources"), environ=env
    )
    return handler.handle(tick(now))


def test_golden_a_due_tick_publishes_the_projection_with_its_own_time(
    tmp_path, monkeypatch
) -> None:
    _, env, now = _home(tmp_path, claim_cells="ticket=ZEP-7")
    bus = _publish(env, now, monkeypatch)
    ((topic, _, value),) = bus.published
    assert topic == TOPIC
    envelope = ModelFloorProjectionEnvelope.model_validate_json(value)
    assert envelope.as_of == now
    assert envelope.projection_id == "work_state"


def test_the_event_names_what_the_publication_did(tmp_path, monkeypatch) -> None:
    _, env, now = _home(tmp_path, claim_cells="ticket=ZEP-7")
    _publish(env, now, monkeypatch)  # sets the publish overlay and the bus in env
    event = _run_scheduled(env, now + timedelta(hours=1))
    assert event is not None
    assert event.publication == "published"


def test_failing_control_a_projection_older_than_its_bound_reads_unknown(
    tmp_path, monkeypatch
) -> None:
    """The stale control: the same publication reads fresh inside its bound and unknown after it."""
    _, env, now = _home(tmp_path, claim_cells="ticket=ZEP-7")
    bus = _publish(env, now, monkeypatch)
    ((topic, _, value),) = bus.published
    bound = READ_OVERLAY.projections["work_state"].max_age_s
    bundle = _floor_bundle(topic, value, now)
    fresh = projection_payload(bundle, "work_state", now + timedelta(seconds=1))
    assert fresh.payload is not None
    stale = projection_payload(bundle, "work_state", now + timedelta(seconds=bound + 1))
    assert stale.payload is None
    assert "bound" in stale.reason


def test_error_a_bus_that_refuses_is_a_reading_and_the_file_is_still_written(
    tmp_path, monkeypatch
) -> None:
    home, env, now = _home(tmp_path, claim_cells="ticket=ZEP-7")

    def refuse(*_args, **_kwargs):
        raise ConnectionError("refused")

    monkeypatch.setattr(adapter, "publish_projection", refuse)
    event = _run_scheduled(env, now)
    assert event is not None
    assert event.publication.startswith("not published (ConnectionError")
    written = _contract("node_work_state_projection_effect")["runtime"]["output"][
        "relpath"
    ]
    assert Path(home / written).is_file()


def test_error_no_publish_overlay_publishes_nothing(tmp_path) -> None:
    _, env, now = _home(tmp_path, claim_cells="ticket=ZEP-7")
    event = _run_scheduled(env, now)
    assert event is not None
    assert event.publication == "not configured"


def test_the_in_memory_profile_publishes_nothing(tmp_path) -> None:
    handler = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="unread"), environ={}
    )
    _, _, now = _home(tmp_path, claim_cells="ticket=ZEP-7")
    event = handler.handle(tick(now))
    assert event is not None
    assert event.publication == ""


def test_a_publication_from_inside_a_running_loop_still_publishes(
    tmp_path, monkeypatch
) -> None:
    """The runtime calls a handler from an event loop; publish_projection starts its own loop, which a running one refuses."""
    _, env, now = _home(tmp_path, claim_cells="ticket=ZEP-7")
    bus = _publish(env, now, monkeypatch)
    before = len(bus.published)

    async def inside_a_loop() -> str:
        projector = adapter.FileSourcesWorkStateProjector(env)
        projector.project(now)
        return projector.publication

    assert asyncio.run(inside_a_loop()) == "published"
    assert len(bus.published) == before + 1
