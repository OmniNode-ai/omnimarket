# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J4 on the floor (OMN-23085): the join test's ledger row, read the way the floor reads it.

The floor lives on a host that is not the one the J4 test runs on, so it cannot read the test's emission journal. It reads the
ledger row the run writes, through the piece_events rules of its overlay, and folds the chains those rows stand for. Every test
runs the real J4 handler, takes the row it appended, turns it into events with the floor's own ``piece_events`` and folds them
with the floor's own receipt fold. The failing control is the same run with a consumer that leaks: the join must not read golden,
and a consumer failure with no scan answer reads broken once the chain deadline passes.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelJoinDef,
    ModelPieceDef,
    ModelPieceEventRule,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)
from omnimarket.nodes.node_secret_reading_effect.adapters.drill_consumers import (
    LeakyLogConsumer,
    LeakyRowConsumer,
)
from tests.unit.nodes.node_secret_reading_effect.test_join_j4 import NOW, Run

pytestmark = pytest.mark.unit

RULES_FILE = Path(__file__).parent / "fixtures" / "floor_piece_events_j4.yaml"
DEADLINE_S = 900


def _rules() -> tuple[ModelPieceEventRule, ...]:
    document = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    return tuple(ModelPieceEventRule.model_validate(item) for item in document)


def _fold(run: Run, *, after: timedelta) -> ModelFloorReceipts:
    rows = [row for line in run.ledger.lines if (row := evidence_row_from_text(line))]
    events = piece_events(rows, _rules())
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW + after,
            emissions=events,
            pieces=(
                ModelPieceDef(piece=4, name="overlays", reading_max_age_s=7200),
                ModelPieceDef(piece=5, name="secrets", reading_max_age_s=7200),
            ),
            joins=(
                ModelJoinDef(
                    id="J4", from_piece=4, to_piece=5, label="overlays to secrets"
                ),
            ),
            freshness_max_age_s=7200,
            error_max_age_s=7200,
            chain_deadline_s=DEADLINE_S,
        )
    )


def _j4(receipts: ModelFloorReceipts) -> str:
    return next(j for j in receipts.joins if j.join == "J4").state


def test_golden_the_rules_are_the_floors_own_rule_model() -> None:
    rules = _rules()
    assert [r.piece for r in rules] == [4, 5]
    assert all(r.golden.lane == "secret-join-j4" for r in rules)


def test_golden_a_clean_run_row_folds_the_join_golden_with_no_finding() -> None:
    run = Run()
    receipts = _fold(run, after=timedelta(minutes=30))
    assert run.result.verdict is not None
    assert run.result.verdict.value == "WORKING"
    assert not receipts.findings, [f.code for f in receipts.findings]
    assert _j4(receipts) == "golden_only"
    joined = next(j for j in receipts.joins if j.join == "J4")
    assert joined.golden_at is not None


def test_golden_the_row_carries_the_cells_the_rules_match() -> None:
    run = Run()
    (line,) = run.ledger.lines
    row = evidence_row_from_text(line)
    assert row is not None
    assert row.lane == "secret-join-j4"
    assert row.cells["src"] == "secret-join-j4"
    assert row.cells["tag"] == "operational"
    assert row.cells["verdict"] == "WORKING"


@pytest.mark.parametrize("leak", [LeakyLogConsumer, LeakyRowConsumer])
def test_error_a_leaking_consumer_does_not_read_golden(leak: type) -> None:
    run = Run(consumer=leak())
    receipts = _fold(run, after=timedelta(minutes=1))
    assert run.result.verdict is not None
    assert run.result.verdict.value == "NOT_WORKING"
    assert _j4(receipts) != "golden_only"
    assert _j4(receipts) != "passed"


@pytest.mark.parametrize("leak", [LeakyLogConsumer, LeakyRowConsumer])
def test_error_a_leaking_consumer_reads_the_join_broken_past_the_deadline(
    leak: type,
) -> None:
    run = Run(consumer=leak())
    receipts = _fold(run, after=timedelta(seconds=DEADLINE_S + 60))
    assert _j4(receipts) == "broken"
    assert [f.code for f in receipts.findings] == ["downstream_missing_typed_failure"]
    assert all(f.subject.startswith("J4 ") for f in receipts.findings)


def test_error_control_the_same_run_without_the_break_never_reads_broken() -> None:
    run = Run()
    receipts = _fold(run, after=timedelta(seconds=DEADLINE_S + 60))
    assert _j4(receipts) == "golden_only"
    assert not receipts.findings
