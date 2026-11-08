# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer is a pure fold with one writer per record: a replay replaces its row, never adds."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.models.seam_reading import (
    EnumSeamCrossingState,
    EnumSeamDisposition,
    EnumSeamSide,
    EnumSeamUnit,
    ModelSeamReading,
    ModelSeamWindow,
)
from omnimarket.nodes.node_seam_conservation_compute.tests.seam_fixtures import (
    WINDOW,
    count,
    ident,
)
from omnimarket.nodes.node_view_seam_reading_reducer.handlers.handler_view_seam_reading_reducer import (
    HandlerViewSeamReadingReducer,
)
from omnimarket.nodes.node_view_seam_reading_reducer.models.model_view_seam_state import (
    ModelViewSeamCountEvent,
    ModelViewSeamFoldRequest,
    ModelViewSeamReadingEvent,
    ModelViewSeamState,
)

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def _reading(read_minute: int, mismatched: int = 0) -> ModelSeamReading:
    return ModelSeamReading(
        seam_id="VS-02",
        window=WINDOW,
        crossings=1,
        mismatched=mismatched,
        unreadable=0,
        read_at=datetime(2026, 11, 2, 12, read_minute, tzinfo=UTC),
    )


def _fold(
    state: ModelViewSeamState,
    event: ModelViewSeamCountEvent | ModelViewSeamReadingEvent,
) -> ModelViewSeamState:
    return (
        HandlerViewSeamReadingReducer()
        .handle(ModelViewSeamFoldRequest(state=state, event=event))
        .state
    )


def test_a_replayed_count_replaces_its_row_and_a_distinct_crossing_adds_one() -> None:
    lines = {
        "lines": (EnumSeamUnit.LINES, [ident("a", EnumSeamDisposition.HANDED_OVER)])
    }
    first = count("VS-02", EnumSeamSide.SENDER, "c-1", lines)
    state = _fold(ModelViewSeamState(), ModelViewSeamCountEvent(count=first))
    state = _fold(state, ModelViewSeamCountEvent(count=first))
    assert len(state.counts) == 1
    other = count("VS-02", EnumSeamSide.SENDER, "c-2", lines)
    state = _fold(state, ModelViewSeamCountEvent(count=other))
    assert len(state.counts) == 2


def test_a_later_reading_replaces_and_an_older_one_does_not() -> None:
    state = _fold(
        ModelViewSeamState(), ModelViewSeamReadingEvent(reading=_reading(11, 2))
    )
    newer = HandlerViewSeamReadingReducer().handle(
        ModelViewSeamFoldRequest(
            state=state, event=ModelViewSeamReadingEvent(reading=_reading(12))
        )
    )
    assert newer.row is not None
    assert newer.row.state is EnumSeamCrossingState.MATCHED
    older = HandlerViewSeamReadingReducer().handle(
        ModelViewSeamFoldRequest(
            state=newer.state, event=ModelViewSeamReadingEvent(reading=_reading(10, 3))
        )
    )
    assert older.row is not None
    assert older.row.state is EnumSeamCrossingState.MATCHED
    assert len(older.state.readings) == 1


def test_a_reading_of_another_window_is_another_row() -> None:
    state = _fold(ModelViewSeamState(), ModelViewSeamReadingEvent(reading=_reading(11)))
    other = ModelSeamReading(
        seam_id="VS-02",
        window=ModelSeamWindow(
            start=datetime(2026, 11, 2, 13, 0, tzinfo=UTC),
            end=datetime(2026, 11, 2, 13, 10, tzinfo=UTC),
        ),
        crossings=1,
        mismatched=0,
        unreadable=0,
        read_at=datetime(2026, 11, 2, 13, 11, tzinfo=UTC),
    )
    state = _fold(state, ModelViewSeamReadingEvent(reading=other))
    assert len(state.readings) == 2


def test_the_contract_declares_a_pure_fold_with_the_exposure_and_no_terminal() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["event_bus"]["publish_topics"] == []
    assert "terminal_events" not in contract.get("runtime_dispatch", {})
    api = contract["projection_api"]
    assert api["order_by"].startswith("seam_id")
    assert api["freshness_column"] == "read_at"
    assert api["table"] == "view_seam_readings"
