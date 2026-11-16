# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A missing-overlay refusal is logged once per distinct cause, not on every replay (OMN-23587).

The two scheduled effects (the floor and the work-state projection) used to raise on every runtime tick while their
overlay was absent; the runtime replays a failed record to the shared tick topic, so every other consumer group logged a
skip line per replay. The golden chain is a due tick with its overlay present; the error chain is the replayed refusal,
a changed cause, and the recovery.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.nodes.node_floor_simple_scheduled_effect.handlers.handler_floor_simple_scheduled import (
    FLOOR_OVERLAY_FILE_ENV,
    SCHEDULE_OVERLAY_FILE_ENV,
    HandlerFloorSimpleScheduled,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    SCHEDULE_OVERLAY_FILE_ENV as WORK_STATE_SCHEDULE_FILE_ENV,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    HandlerWorkStateProjectionScheduled,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    FIXTURE as WORK_STATE_FIXTURE,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import T0, tick

pytestmark = pytest.mark.unit

FLOOR_NODE = "node_floor_simple_scheduled_effect"
WORK_STATE_NODE = "node_work_state_projection_scheduled_effect"
FLOOR_SCHEDULE_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "nodes/node_floor_simple_scheduled_effect/fixtures/overlays"
    / FLOOR_NODE
    / "overlay.yaml"
)
REFUSED = "scheduled overlay refused"
RECOVERED = "scheduled overlay recovered"
REPLAYS = 5


def lines(caplog: pytest.LogCaptureFixture, prefix: str, node: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith(prefix) and f"node={node} " in r.getMessage() + " "
    ]


def floor_files(tmp_path: Path) -> dict[str, str]:
    floor = tmp_path / "floor_overlay.yaml"
    floor.write_text(
        yaml.safe_dump(overlay_text("node_floor_simple_compute")), encoding="utf-8"
    )
    return {
        SCHEDULE_OVERLAY_FILE_ENV: str(FLOOR_SCHEDULE_FIXTURE),
        FLOOR_OVERLAY_FILE_ENV: str(floor),
    }


def test_golden_a_due_tick_with_the_overlays_present_emits_and_logs_no_refusal(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.INFO)
    floor = HandlerFloorSimpleScheduled(environ=floor_files(tmp_path))
    assert floor.handle(tick(T0)) is not None
    work = HandlerWorkStateProjectionScheduled(
        environ={WORK_STATE_SCHEDULE_FILE_ENV: str(WORK_STATE_FIXTURE)}
    )
    assert work.handle(tick(T0)) is not None
    for node in (FLOOR_NODE, WORK_STATE_NODE):
        assert lines(caplog, REFUSED, node) == []
        assert lines(caplog, RECOVERED, node) == []


@pytest.mark.parametrize(
    ("handler", "node", "setting"),
    [
        (
            lambda: HandlerFloorSimpleScheduled(environ={}),
            FLOOR_NODE,
            "ONEX_FLOOR_SCHEDULE_OVERLAY",
        ),
        (
            lambda: HandlerWorkStateProjectionScheduled(environ={}),
            WORK_STATE_NODE,
            "ONEX_WORK_STATE_SCHEDULE_OVERLAY",
        ),
    ],
)
def test_error_one_failed_tick_replayed_five_times_logs_one_error_line(
    caplog: pytest.LogCaptureFixture, handler, node: str, setting: str
) -> None:
    caplog.set_level(logging.INFO)
    h = handler()
    failed = tick(T0)
    for _ in range(1 + REPLAYS):
        assert h.handle(failed) is None
    errors = lines(caplog, REFUSED, node)
    assert len(errors) == 1
    assert setting in errors[0]
    assert "error_type=NodeOverlayNotFoundError" in errors[0]
    # a later tick with the same cause is still one line
    assert h.handle(tick(T0)) is None
    assert len(lines(caplog, REFUSED, node)) == 1


def test_error_a_changed_cause_logs_again_and_recovery_logs_the_first_success_once(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.INFO)
    files = floor_files(tmp_path)
    environ: dict[str, str] = {}
    h = HandlerFloorSimpleScheduled(environ=environ)
    assert h.handle(tick(T0)) is None  # cause 1: no schedule overlay
    assert h.handle(tick(T0)) is None
    environ[SCHEDULE_OVERLAY_FILE_ENV] = files[SCHEDULE_OVERLAY_FILE_ENV]
    assert h.handle(tick(T0)) is None  # cause 2: the floor overlay is missing
    assert h.handle(tick(T0)) is None
    errors = lines(caplog, REFUSED, FLOOR_NODE)
    assert len(errors) == 2
    assert SCHEDULE_OVERLAY_FILE_ENV in errors[0]
    assert FLOOR_OVERLAY_FILE_ENV in errors[1]
    assert lines(caplog, RECOVERED, FLOOR_NODE) == []
    environ[FLOOR_OVERLAY_FILE_ENV] = files[FLOOR_OVERLAY_FILE_ENV]
    assert h.handle(tick(T0)) is not None  # the overlay appeared: the first success
    assert len(lines(caplog, RECOVERED, FLOOR_NODE)) == 1
    assert h.handle(tick(T0)) is None  # inside the interval
    assert len(lines(caplog, RECOVERED, FLOOR_NODE)) == 1
    assert len(lines(caplog, REFUSED, FLOOR_NODE)) == 2


def test_error_the_work_state_overlay_appearing_later_is_picked_up_on_the_next_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    environ: dict[str, str] = {}
    h = HandlerWorkStateProjectionScheduled(environ=environ)
    for _ in range(3):
        assert h.handle(tick(T0)) is None
    environ[WORK_STATE_SCHEDULE_FILE_ENV] = str(WORK_STATE_FIXTURE)
    assert h.handle(tick(T0)) is not None
    assert len(lines(caplog, REFUSED, WORK_STATE_NODE)) == 1
    assert len(lines(caplog, RECOVERED, WORK_STATE_NODE)) == 1


def test_error_the_latch_logs_a_refusal_that_follows_a_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    latch = ScheduledOverlayRefusalLatch("node_probe")
    cause = NodeOverlayNotFoundError("node_probe.x is not configured")
    assert latch.refuse(cause) is not None
    assert latch.refuse(cause) is None
    assert latch.recovered() is True
    assert latch.recovered() is False
    assert latch.refuse(cause) is not None
    assert len(lines(caplog, REFUSED, "node_probe")) == 2
    assert len(lines(caplog, RECOVERED, "node_probe")) == 1
