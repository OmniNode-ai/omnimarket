# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC6, the caller: the recorded readings of a finalized window become the tick's typed field,
and VS-17 is measured from the tick's result."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.models.seam_reading import (
    EnumSeamSide,
    ModelSeamReading,
    ModelSeamWindow,
)
from omnimarket.nodes.node_seam_conservation_compute.handlers.handler_seam_readings_feed import (
    HandlerSeamReadingsFeed,
)
from omnimarket.nodes.node_seam_conservation_compute.models.model_seam_readings_feed import (
    ModelSeamReadingsFeedRequest,
)
from omnimarket.nodes.node_seam_conservation_compute.tests.seam_fixtures import WINDOW

pytestmark = pytest.mark.unit

EXPECTED = tuple(f"VS-{n:02d}" for n in range(1, 17))
READ_AT = datetime(2026, 11, 2, 12, 11, tzinfo=UTC)


def _reading(seam_id: str, window: ModelSeamWindow = WINDOW) -> ModelSeamReading:
    return ModelSeamReading(
        seam_id=seam_id,
        window=window,
        crossings=1,
        mismatched=0,
        unreadable=0,
        read_at=READ_AT,
    )


def test_seam_readings_feed_builds_the_tick_field_from_the_recorded_readings() -> None:
    result = HandlerSeamReadingsFeed().handle(
        ModelSeamReadingsFeedRequest(
            window=WINDOW,
            finalized=True,
            publish_steps_ok=True,
            expected_seam_ids=EXPECTED,
            declared_seam_ids=("VS-03",),
            recorded=tuple(_reading(s) for s in EXPECTED[:-1]),
            tick_id="tick-1",
            now=READ_AT,
        )
    )
    facts = result.facts
    assert facts.expected_seam_ids == EXPECTED
    assert len(facts.readings) == 15
    assert facts.declared_seam_ids == ("VS-03",)


def test_seam_readings_feed_drops_a_reading_of_another_window() -> None:
    other = ModelSeamWindow(
        start=datetime(2026, 11, 2, 11, 0, tzinfo=UTC),
        end=datetime(2026, 11, 2, 11, 10, tzinfo=UTC),
    )
    result = HandlerSeamReadingsFeed().handle(
        ModelSeamReadingsFeedRequest(
            window=WINDOW,
            finalized=True,
            publish_steps_ok=True,
            expected_seam_ids=EXPECTED,
            declared_seam_ids=(),
            recorded=(_reading("VS-01", other), _reading("VS-02")),
            tick_id="tick-1",
            now=READ_AT,
        )
    )
    assert [r.seam_id for r in result.facts.readings] == ["VS-02"]


def test_seam_readings_feed_measures_vs17_sender_ids_against_consumed_ids() -> None:
    handler = HandlerSeamReadingsFeed()
    request = ModelSeamReadingsFeedRequest(
        window=WINDOW,
        finalized=True,
        publish_steps_ok=True,
        expected_seam_ids=EXPECTED,
        declared_seam_ids=(),
        recorded=tuple(_reading(s) for s in EXPECTED[:-1]),
        tick_id="tick-1",
        now=READ_AT,
    )
    counts = handler.vs17_counts(
        request, consumed_ids=EXPECTED[:-1], missing_ids=(), unknown_ids=("VS-16",)
    )
    assert {c.side for c in counts} == {EnumSeamSide.SENDER, EnumSeamSide.RECEIVER}
    sender = next(c for c in counts if c.side is EnumSeamSide.SENDER)
    receiver = next(c for c in counts if c.side is EnumSeamSide.RECEIVER)
    assert {m.measure_id for m in sender.measures} == {"readings", "findings"}
    readings = next(m for m in receiver.measures if m.measure_id == "readings")
    assert len(readings.ids) == 15
    findings = next(m for m in receiver.measures if m.measure_id == "findings")
    assert [i.id for i in findings.ids] == ["seam:VS-16"]
    sent = next(m for m in sender.measures if m.measure_id == "findings")
    assert [i.id for i in sent.ids] == ["seam:VS-16"]
