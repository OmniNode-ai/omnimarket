# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the scheduled floor compute (OMN-23072): the schedule gate, the document, the refusals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.floor_17a.model_floor_document_event import (
    ModelFloorScheduleOverlay,
)
from omnimarket.nodes.node_floor_simple_scheduled_effect.handlers.handler_floor_simple_scheduled import (
    HandlerFloorSimpleScheduled,
)
from tests.nodes.node_floor_page_api_effect.support import floor_overlay

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent
    / "fixtures/overlays/node_floor_simple_scheduled_effect/overlay.yaml"
)
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_simple_scheduled_effect/contract.yaml"
)
T0 = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def handler() -> HandlerFloorSimpleScheduled:
    return HandlerFloorSimpleScheduled(
        schedule=ModelFloorScheduleOverlay.model_validate(
            yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
        ),
        floor_overlay=floor_overlay(),
    )


def test_golden_a_due_tick_emits_the_floor_document_for_that_tick() -> None:
    first = tick(T0)
    event = handler().handle(first)
    assert event is not None
    assert event.computed_at == T0
    assert event.tick_id == str(first.tick_id)
    assert event.document.schema_id == "omni.floor-simple/1"


def test_golden_the_schedule_gate_runs_once_per_interval() -> None:
    h = handler()
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=59))) is None
    assert h.handle(tick(T0 + timedelta(seconds=60))) is not None


def test_error_a_skipped_tick_does_not_move_the_last_run() -> None:
    h = handler()
    h.handle(tick(T0))
    assert h.handle(tick(T0 + timedelta(seconds=30))) is None
    # 30 s after the skipped tick is 60 s after the last run: due.
    assert h.handle(tick(T0 + timedelta(seconds=60))) is not None


def test_error_inputs_nothing_read_are_drawn_unread_never_zero() -> None:
    event = handler().handle(tick(T0))
    assert event is not None
    number_values = [n.value for n in event.document.numbers]
    assert "0" not in number_values


def test_error_no_schedule_overlay_refuses_naming_the_setting() -> None:
    h = HandlerFloorSimpleScheduled(environ={})
    with pytest.raises(NodeOverlayNotFoundError, match="ONEX_FLOOR_SCHEDULE_OVERLAY"):
        h.handle(tick(T0))


def test_error_a_zero_interval_overlay_is_refused() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        ModelFloorScheduleOverlay.model_validate(
            {
                "run_interval_seconds": 0,
                "reading_source": "unread",
                "floor_overlay_id": "x",
            }
        )


def test_the_contract_subscribes_to_the_runtime_tick_and_names_its_terminal_topic() -> (
    None
):
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert (
        contract["input_subscriptions"][0]["topic"]
        == "onex.intent.platform.runtime-tick.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
