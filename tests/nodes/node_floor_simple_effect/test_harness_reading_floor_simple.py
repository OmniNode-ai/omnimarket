# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The central test harness's stored result is the reading of its piece on the floor (OMN-22478).

The harness node writes one JSON document after each run. The floor reads it as the reading of piece 8, next to the
reading writer's rows. A result that is missing, unreadable or past its own maximum age adds no working reading.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import ModelFloorPieceRow, ModelFloorSimple
from tests.nodes.node_floor_simple_effect.support import NOW, build_home, iso
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)

pytestmark = pytest.mark.unit

RESULT = ".onex_state/readings/central-test-harness.drill.json"
PIECE = 8


def result(state: str = "WORKING", *, age_s: int = 120) -> dict[str, object]:
    moment = NOW - timedelta(seconds=age_s)
    return {
        "piece": "central-test-harness",
        "state": state,
        "probed_at": iso(moment),
        "published_at": iso(moment),
        "max_age_seconds": 7200,
    }


def piece_row(doc: ModelFloorSimple) -> ModelFloorPieceRow:
    return next(p for p in doc.pieces if p.piece == PIECE)


def put_result(home: Path, body: object) -> None:
    path = home / RESULT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        body if isinstance(body, str) else json.dumps(body), encoding="utf-8"
    )


def test_golden_a_working_harness_result_is_the_reading_of_piece_8(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    put_result(home, result("WORKING", age_s=120))
    assert run(home).ok
    row = piece_row(written(home))
    assert row.reading_state == "WORKING"
    assert row.reading_age_s is not None
    assert 100 <= row.reading_age_s <= 200
    assert row.reading_max_age_s > 0


def test_error_a_harness_result_that_reads_not_working_is_drawn_not_working(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    put_result(home, result("NOT WORKING"))
    assert run(home).ok
    assert piece_row(written(home)).reading_state == "NOT WORKING"


@pytest.mark.parametrize("body", ["{ not json", json.dumps({"state": "WORKING"})])
def test_error_an_unreadable_harness_result_gives_the_piece_no_reading(
    tmp_path: Path, body: str
) -> None:
    home = build_home(tmp_path)
    put_result(home, body)
    assert run(home).ok
    assert piece_row(written(home)).reading_state is None


def test_error_without_a_harness_result_the_piece_has_no_reading(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    assert run(home).ok
    assert piece_row(written(home)).reading_state is None
