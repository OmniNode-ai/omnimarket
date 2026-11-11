# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the J0 join runner (OMN-23022), each pinned by a test that fails when either side changes shape.

Seam 1, the floor simple node's contract: the chain the runner writes (``node_floor_simple_effect:join_j0``) must be one of the
floor's ``pipeline_chains`` and the join end the floor judges J0 at, or the floor never reads the chain the runner writes.
Seam 2, the receipt fold: the journal a run writes, folded by ``HandlerFloorReceipt`` with the floor's own ``join_ends``, reads J0
``passed``; with the join end gone (the old cycle end) it does not.
Seam 3, the publish contract: the step ``join_j0`` runs before the record step.
Seam 4, the overlay: the overlay example's chain names are the contract's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelJoinEnd,
    ModelPieceDef,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    load_runtime as load_publish_runtime,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    load_runtime,
)
from omnimarket.nodes.node_join_j0_effect import HandlerJoinJ0
from omnimarket.nodes.node_join_j0_effect.handlers.handler_join_j0 import (
    refuse_unjudged_join_end,
)
from omnimarket.nodes.node_join_j0_effect.models import ModelJoinJ0Command
from omnimarket.nodes.node_join_j0_effect.overlay import ModelJoinJ0Overlay

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_join_j0_effect/overlay.example.yaml"
)
CHAIN = "node_floor_simple_effect:join_j0"
RUNTIME = load_runtime()


def _overlay() -> ModelJoinJ0Overlay:
    return ModelJoinJ0Overlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


def _journal(tmp_path: Path) -> InMemoryEmissionSink:
    record = tmp_path / "floor-simple.json"
    record.write_text(
        '{"generated_at": "2026-11-11T11:59:30+00:00", "freshness_max_age_s": 900, '
        '"pieces": [{"piece": 1, "reading_age_s": 5}]}',
        encoding="utf-8",
    )
    sink = InMemoryEmissionSink()
    HandlerJoinJ0(
        overlay=_overlay(),
        sink=sink,
        floor_record_path=record,
        backend="in_memory",
        clock=lambda: NOW,
    ).handle(ModelJoinJ0Command(write=True))
    return sink


def _j0_state(sink: InMemoryEmissionSink, ends: tuple[ModelJoinEnd, ...]) -> str:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW + timedelta(seconds=1),
            emissions=tuple(sink.events),
            pieces=tuple(
                ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400)
                for n in range(1, 19)
            ),
            joins=(
                ModelJoinDef(
                    id="J0", from_piece=17, to_piece=1, label="step 0 to the code graph"
                ),
            ),
            join_ends=ends,
            freshness_max_age_s=7200,
            error_max_age_s=86400,
            chain_deadline_s=900,
            judged_to_pieces=(1, 17),
        )
    )
    return receipts.joins[0].state


def test_seam_floor_contract_reads_the_runners_chain_as_pipeline_chain_and_j0_end() -> (
    None
):
    assert CHAIN in RUNTIME.pipeline_chains
    assert [(e.join, e.node) for e in RUNTIME.join_ends if e.join == "J0"] == [
        ("J0", CHAIN)
    ]
    refuse_unjudged_join_end("J0", CHAIN, RUNTIME.pipeline_chains, RUNTIME.join_ends)


def test_seam_the_overlay_example_names_the_chain_the_contract_declares() -> None:
    assert _overlay().join.from_node == CHAIN


def test_seam_the_runner_refuses_when_either_side_drops_the_declaration() -> None:
    no_end = tuple(e for e in RUNTIME.join_ends if e.join != "J0")
    with pytest.raises(ValueError, match="no join_end"):
        refuse_unjudged_join_end("J0", CHAIN, RUNTIME.pipeline_chains, no_end)
    no_chain = tuple(c for c in RUNTIME.pipeline_chains if c != CHAIN)
    with pytest.raises(ValueError, match="not among the chains"):
        refuse_unjudged_join_end("J0", CHAIN, no_chain, RUNTIME.join_ends)


def test_seam_the_journal_a_run_writes_reads_j0_passed_with_the_floors_own_join_ends(
    tmp_path: Path,
) -> None:
    assert _j0_state(_journal(tmp_path), RUNTIME.join_ends) == "passed"


def test_seam_control_with_the_old_cycle_end_the_runner_chains_are_not_read(
    tmp_path: Path,
) -> None:
    old = (
        ModelJoinEnd(join="J0", node="node_floor_simple_effect:run_floor_simple_cycle"),
    )
    assert _j0_state(_journal(tmp_path), old) == "not_run"


def test_seam_the_publish_contract_runs_the_step_before_the_record_step() -> None:
    names = [s.name for s in load_publish_runtime().steps]
    assert names.index("join_j0") < names.index("factory_floor")
