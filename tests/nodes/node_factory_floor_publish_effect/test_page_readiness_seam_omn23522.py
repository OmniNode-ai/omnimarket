# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The main page's readiness wording reads five cells of the floor record; this seam holds the two ends together (OMN-23522).

The page is JavaScript the models cannot import and the models are Python the page cannot call, so nothing
else ties the two: when a field is renamed on either side, the served board goes silently blank and only
this test goes red. The integration value set is held the same way: a value the model adds fails until the
page's word table is revisited.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from omnimarket.models.floor_17a.model_floor_17a import (
    ModelFloorJoinRow,
    ModelFloorPieceRow,
    ModelFloorReadiness,
    ModelFloorSimple,
)

pytestmark = pytest.mark.unit

PAGE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/simple.html"
).read_text(encoding="utf-8")

# The five reads the page's readiness wording makes of the record, with the model field each one lands on.
PAGE_READS: tuple[tuple[str, type[BaseModel], str], ...] = (
    ("p.integration", ModelFloorPieceRow, "integration"),
    ("p.blocking_joins", ModelFloorPieceRow, "blocking_joins"),
    ("p.ready", ModelFloorPieceRow, "ready"),
    ("j.counted", ModelFloorJoinRow, "counted"),
    ("doc.readiness.text", ModelFloorReadiness, "text"),
)


def missing_fields(
    model_fields_by_class: dict[type[BaseModel], dict[str, FieldInfo]],
) -> list[str]:
    """The page reads whose field the matching model no longer carries; empty is a held seam."""
    return [
        expr
        for expr, cls, field in PAGE_READS
        if field not in model_fields_by_class[cls]
    ]


def _integration_table_keys() -> set[str]:
    """The keys of the page's INTEGRATION_WORD table, read off its declaration line."""
    line = next(
        line
        for line in PAGE.splitlines()
        if line.startswith("const INTEGRATION_WORD = {")
    )
    return set(re.findall(r"[{,]\s*(\w+)\s*:", line))


def test_golden_every_record_field_the_page_reads_for_readiness_is_in_the_model() -> (
    None
):
    for expr, cls, field in PAGE_READS:
        assert expr in PAGE
        assert field in cls.model_fields
    # The readiness block is drawn only for a record that has one: the field may be None, never absent.
    assert "readiness" in ModelFloorSimple.model_fields
    assert ModelFloorSimple.model_fields["readiness"].is_required() is False


def test_golden_the_pages_integration_table_covers_the_models_integration_values() -> (
    None
):
    values = set(get_args(ModelFloorPieceRow.model_fields["integration"].annotation))
    # A value the model adds fails here until the page's word table is revisited.
    assert values == {
        "ready",
        "working_not_integrated",
        "not_working",
        "not_started",
    }
    keys = _integration_table_keys()
    assert {"ready", "working_not_integrated"} <= keys
    # The other two keep the reading's own word: the page looks them up, misses, and falls through.
    assert "not_working" not in keys
    assert "not_started" not in keys


def test_error_a_field_renamed_in_the_model_fails_the_seam() -> None:
    real = {cls: cls.model_fields for _, cls, _ in PAGE_READS}
    assert missing_fields(real) == []
    # The named break: the model renames its integration field under the page's table.
    renamed = {
        ("integration_state" if name == "integration" else name): info
        for name, info in ModelFloorPieceRow.model_fields.items()
    }
    assert missing_fields({**real, ModelFloorPieceRow: renamed}) == ["p.integration"]


def test_golden_the_computed_record_carries_what_the_page_draws() -> None:
    """The record the compute node writes, as the served JSON, holds every cell the page reads (a broken J2 piece pair)."""
    from tests.nodes.node_floor_simple_compute.test_readiness_omn23081 import board

    served = json.loads(board({"J2": "broken"}).model_dump_json())
    pieces = {p["piece"]: p for p in served["pieces"]}
    assert pieces[1]["integration"] == "ready"
    assert pieces[1]["ready"] is True
    assert pieces[2]["integration"] == "working_not_integrated"
    assert pieces[2]["blocking_joins"] == ["J2"]
    assert pieces[7]["integration"] == "not_working"
    joins = {j["id"]: j for p in served["pieces"] for j in p["joins"]}
    assert joins["J2"]["counted"] is True
    assert "counted" in joins["J14"]
    # The page draws each of those cells: a record that carries them and a page that ignores them is the gap this closes.
    for expr, _cls, _field in PAGE_READS:
        assert expr in PAGE
    assert "pieceWord(p)" in PAGE
    # The line under the strip is the record's own text; it names the pieces that work and are not integrated.
    assert "work but are not integrated (pieces 2, 3)" in served["readiness"]["text"]
