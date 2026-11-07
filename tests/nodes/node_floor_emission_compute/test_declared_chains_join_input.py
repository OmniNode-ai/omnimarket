# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A join reads the pipeline chains its piece's node declares, not the node's per-row chains (OMN-22478).

The floor cycle node emits its own pipeline chains (the cycle and its steps) and one chain per lab source row it reads
(the overlay store, the deploy agent). A row that reads a bad value ends in an error chain, and J0 (piece 17 to 1) then
demanded a typed ``upstream_`` failure from piece 1 for each; no downstream piece can give one. The join now reads the
chains the floor cycle node declares in its contract.

Golden chain: today's 93 broken chains, rebuilt below from the measured counts (64 floor-row chains under J0, J5 14,
J2 8, J4 5, J3 1, one cycle that never ended): the 64 row chains stop being counted and the 29 real ones still fail.
Error chain (the failing controls): a join with no declared chains still counts all 93; a real cycle failure under J0
still fails; a malformed row chain is still named by its own finding; the declared list is the one the code emits.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
)
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelFloorSimpleOverlay,
    ModelLabSourceRead,
    ModelLabSubject,
    ModelNodeEmission,
    ModelPieceChains,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers import (
    handler_floor_simple_tick as tick_module,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    EMIT_CYCLE,
    HandlerFloorSimpleTick,
    load_runtime,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    OVERLAY_STORE_EMITTER,
    overlay_store_events,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources_deploy_agent import (
    DEPLOY_AGENT_EMITTER,
    deploy_agent_events,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 7, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=1)
OVERLAY = ModelFloorSimpleOverlay.model_validate(
    overlay_text("node_floor_simple_compute")
)
FLOOR_PIECE = OVERLAY.rebuild.lab_state_piece

# The measured counts of the 93 broken chains read from the served floor record: findings per join, and the rows.
ROW_CHAINS = {
    "overlay_store_not_fresh": 34,
    "deploy_agent_failing": 26,
    "deploy_agent_unread": 3,
}
# The floor record names 63 of the 64 J0 chains by failure code; the 64th is drawn here as one more unread overlay store.
ROW_CHAINS_UNCODED = 1
JOIN_FAILURES = {"J2": 8, "J3": 1, "J4": 5, "J5": 14}
J0_ROW_CHAINS = sum(ROW_CHAINS.values()) + ROW_CHAINS_UNCODED
PIPELINE_NODES = load_runtime().pipeline_chains


def _row_read(code: str) -> ModelLabSourceRead:
    if code.endswith("unread"):
        return ModelLabSourceRead(id="row", status="unread", reason="no read")
    return ModelLabSourceRead(
        id="row",
        status="read",
        read_at=T0,
        subjects=(ModelLabSubject(name="subject", ok=False, note="failing"),),
    )


def row_chain_events(n: int) -> list[ModelNodeEmission]:
    """The row chains exactly as the cycle's own emitters write them: a start and a typed failure, one id per read."""
    out: list[ModelNodeEmission] = []
    index = 0
    plan = [*ROW_CHAINS.items(), ("overlay_store_unread", ROW_CHAINS_UNCODED)]
    for code, count in plan:
        for _ in range(count):
            at = T0 + timedelta(seconds=index)
            cid = f"row-{index}"
            index += 1
            if code.startswith("overlay_store"):
                out.extend(overlay_store_events(_row_read(code), FLOOR_PIECE, at, cid))
            else:
                out.extend(deploy_agent_events(_row_read(code), FLOOR_PIECE, at, cid))
    assert len(out) == 2 * n
    return out


def piece_failure(piece: int, node: str, cid: str, at: int) -> list[ModelNodeEmission]:
    """One real chain of a pipeline node that failed with a typed code; its downstream never answers."""
    base = {"node": node, "piece": piece, "correlation_id": cid}
    return [
        ModelNodeEmission.model_validate(
            {**base, "phase": "start", "emitted_at": T0 + timedelta(seconds=at)}
        ),
        ModelNodeEmission.model_validate(
            {
                **base,
                "phase": "failure",
                "emitted_at": T0 + timedelta(seconds=at + 1),
                "failure_code": "source_unreadable",
            }
        ),
    ]


def todays_chains() -> list[ModelNodeEmission]:
    events = row_chain_events(J0_ROW_CHAINS)
    for join, count in JOIN_FAILURES.items():
        from_piece = int(join[1:])
        for i in range(count):
            events += piece_failure(from_piece, f"node_p{from_piece}", f"{join}-{i}", i)
    # The cycle the kickstart killed: a start and nothing after it.
    events.append(
        ModelNodeEmission.model_validate(
            {
                "node": EMIT_CYCLE,
                "piece": FLOOR_PIECE,
                "correlation_id": "killed-cycle",
                "phase": "start",
                "emitted_at": T0,
            }
        )
    )
    return events


def fold(
    events: list[ModelNodeEmission],
    declared: tuple[ModelPieceChains, ...] = (),
) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW,
            emissions=tuple(events),
            pieces=OVERLAY.pieces,
            joins=OVERLAY.joins,
            freshness_max_age_s=OVERLAY.freshness_max_age_s,
            error_max_age_s=OVERLAY.error_receipt_max_age_s,
            chain_deadline_s=OVERLAY.chain_deadline_s,
            judged_to_pieces=tuple(range(1, 18)),
            declared_chains=declared,
        )
    )


def declared_floor() -> tuple[ModelPieceChains, ...]:
    return (ModelPieceChains(piece=FLOOR_PIECE, nodes=PIPELINE_NODES),)


def by_subject(receipts: ModelFloorReceipts) -> Counter[str]:
    return Counter(f.subject.split(" piece ")[0] for f in receipts.findings)


def test_golden_todays_93_broken_chains_drop_the_64_row_chains_and_keep_the_29_real_ones() -> (
    None
):
    before = fold(todays_chains())
    after = fold(todays_chains(), declared_floor())
    assert len(before.findings) == 93
    assert by_subject(before)["J0"] == J0_ROW_CHAINS == 64
    assert len(after.findings) == 29
    counts = by_subject(after)
    assert counts["J0"] == 0
    assert {j: counts[j] for j in JOIN_FAILURES} == JOIN_FAILURES
    # The cycle that never ended is still named, by its own chain finding.
    assert [f.code for f in after.findings if "J" not in f.subject.split(" ")[0]] == [
        "start_without_terminal"
    ]


def test_golden_the_floor_pieces_own_receipt_is_the_same_with_and_without_the_declaration() -> (
    None
):
    before = fold(todays_chains())
    after = fold(todays_chains(), declared_floor())
    assert before.pieces == after.pieces
    assert before.chains == after.chains


def test_error_control_a_join_with_no_declaration_still_counts_every_row_chain() -> (
    None
):
    assert by_subject(fold(todays_chains()))["J0"] == 64


def test_error_control_declaring_the_row_nodes_brings_back_the_64_failures() -> None:
    declared = (
        ModelPieceChains(
            piece=FLOOR_PIECE,
            nodes=(*PIPELINE_NODES, OVERLAY_STORE_EMITTER, DEPLOY_AGENT_EMITTER),
        ),
    )
    assert by_subject(fold(todays_chains(), declared))["J0"] == 64


def test_error_a_real_pipeline_failure_still_breaks_j0() -> None:
    events = [
        *row_chain_events(J0_ROW_CHAINS),
        *piece_failure(
            FLOOR_PIECE, "node_floor_simple_compute:compute_floor_simple", "real", 5
        ),
    ]
    receipts = fold(events, declared_floor())
    assert [f.code for f in receipts.findings] == ["downstream_missing_typed_failure"]
    assert receipts.findings[0].correlation_id == "real"
    j0 = next(j for j in receipts.joins if j.join == "J0")
    assert j0.state == "broken"


def test_golden_with_only_row_chains_failing_j0_is_not_broken() -> None:
    receipts = fold(row_chain_events(J0_ROW_CHAINS), declared_floor())
    assert receipts.findings == ()
    assert next(j for j in receipts.joins if j.join == "J0").state == "not_run"


def test_error_a_malformed_row_chain_is_still_named_by_its_own_finding() -> None:
    bad = ModelNodeEmission.model_validate(
        {
            "node": OVERLAY_STORE_EMITTER,
            "piece": FLOOR_PIECE,
            "correlation_id": "bad-row",
            "phase": "success",
            "emitted_at": T0,
        }
    )
    receipts = fold([bad], declared_floor())
    assert [f.code for f in receipts.findings] == ["success_without_start"]


def test_error_the_declaration_names_a_piece_only_for_the_piece_it_was_given() -> None:
    other = piece_failure(5, "node_p5", "p5", 0)
    receipts = fold([*row_chain_events(J0_ROW_CHAINS), *other], declared_floor())
    assert by_subject(receipts) == Counter({"J5": 1})


def test_golden_the_tick_hands_the_receipt_the_contracts_declared_chains() -> None:
    sink = InMemoryEmissionSink()
    for event in todays_chains():
        sink.emit(event)
    rt = load_runtime()
    # A go of 6 admits pieces 1 to 6, so J0 (into piece 1) and J5 (into piece 6) are judged as they are on the floor.
    receipts = HandlerFloorSimpleTick._receipts(sink, OVERLAY, NOW, rt, 6, ())
    assert len(receipts.findings) == 29
    assert by_subject(receipts)["J0"] == 0
    assert by_subject(receipts)["J5"] == JOIN_FAILURES["J5"]


def test_error_every_chain_the_cycle_emits_is_declared_in_the_contract() -> None:
    """A node added to the cycle and left out of the contract would silently stop being judged by J0."""
    source = Path(tick_module.__file__).read_text(encoding="utf-8")
    emitted = set(re.findall(r'"(node_[a-z_]+:[a-z_]+)"', source))
    assert emitted == set(PIPELINE_NODES)
    assert OVERLAY_STORE_EMITTER not in PIPELINE_NODES
    assert DEPLOY_AGENT_EMITTER not in PIPELINE_NODES
