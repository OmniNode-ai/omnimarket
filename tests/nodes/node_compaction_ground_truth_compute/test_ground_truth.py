# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ground truth derived from a synthetic ledger: exact sets per ``now``, absences asserted."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_compaction_ground_truth_compute.handlers.handler_ground_truth import (
    HandlerCompactionGroundTruth,
)
from omnimarket.nodes.node_compaction_ground_truth_compute.models import (
    EnumExpectedConduct,
    EnumGroundTruthKind,
    ModelGroundTruthRequest,
    ModelGroundTruthResult,
)
from tests.nodes.node_compaction_ground_truth_compute import fixture_ledger as f

pytestmark = pytest.mark.unit
K = EnumGroundTruthKind


def derive(
    now: str,
    prefix: tuple[str, ...] = f.PREFIX,
    extra_lanes: tuple[str, ...] = (),
) -> ModelGroundTruthResult:
    request = ModelGroundTruthRequest(
        session_id="session-a",
        ledger_prefix=prefix,
        now=now,
        window_start=f.WINDOW_START,
        orchestrator_lanes=("orch-a",),
        spent_consent_stamps=(f.C2,),
        extra_lanes=extra_lanes,
    )
    return HandlerCompactionGroundTruth().handle(request)


def stamps_by_kind(result: ModelGroundTruthResult) -> dict[K, set[frozenset[str]]]:
    """Per kind, one frozenset of row stamps per item."""
    out: dict[K, set[frozenset[str]]] = {}
    for item in result.items:
        out.setdefault(item.kind, set()).add(frozenset(r.stamp for r in item.rows))
    return out


def fs(*stamps: str) -> frozenset[str]:
    return frozenset(stamps)


def test_expected_set_at_t1() -> None:
    got = stamps_by_kind(derive(f.T1))
    assert got == {
        K.RULING: {fs(f.R1, f.R2), fs(f.R4), fs(f.R5, f.CORR5)},
        K.OPERATOR_CONSENT: {fs(f.C1)},
        K.OPEN_CLAIM: {fs(f.CLAIM_OLD), fs(f.CLAIM_Y)},
        K.HOLD: {fs(f.H1), fs(f.H2), fs(f.H3)},
        K.OPEN_ASK: {fs(f.M1), fs(f.M2), fs(f.M3)},
        K.PROMISE_SENT: {fs(f.M4)},
        K.DRAFT_UNSENT: {fs(f.DRAFT1), fs(f.DRAFT2)},
        K.UNACKED_MSG: {fs(f.M5), fs(f.M6)},
    }


def test_expected_set_at_t2() -> None:
    got = stamps_by_kind(derive(f.T2))
    assert got == {
        K.RULING: {fs(f.R1, f.R2), fs(f.R4), fs(f.R5, f.CORR5)},
        K.OPERATOR_CONSENT: {fs(f.C1)},
        K.OPEN_CLAIM: {fs(f.CLAIM_OLD), fs(f.CLAIM_Y)},
        K.HOLD: {fs(f.H1)},
        K.OPEN_ASK: {fs(f.M1)},
        K.PROMISE_SENT: {fs(f.M4), fs(f.M8)},
        K.DRAFT_UNSENT: {fs(f.DRAFT1)},
        K.UNACKED_MSG: {fs(f.M5)},
    }


def test_expected_set_at_t3() -> None:
    got = stamps_by_kind(derive(f.T3))
    assert got[K.RULING] == {fs(f.R1, f.R2), fs(f.R4), fs(f.R5, f.CORR5), fs(f.R6)}
    assert got[K.OPEN_CLAIM] == {fs(f.CLAIM_OLD)}


def test_closed_irrelevant_and_out_of_scope_rows_are_absent() -> None:
    result = derive(f.T2)
    present = {r.stamp for item in result.items for r in item.rows}
    absent = {
        f.PRE_RULING: "ruling before the window",
        f.R3: "superseded ruling",
        f.CLAIM_X: "claim closed by a TERMINAL",
        f.TERM_X: "the closing TERMINAL itself",
        f.CLAIM_OTHER: "claim of another family",
        f.C2: "spent consent",
        f.H2: "released hold",
        f.H3: "expired hold",
        f.H4: "hold of another family",
        f.M2: "ask answered by an ACK",
        f.M3: "ask answered by a message naming it",
        f.M6: "message acknowledged",
        f.M7: "message to another family",
        f.M9: "message to a child lane, not the session lane",
        f.DRAFT2: "draft sent by a later SENT-ON-BEHALF row",
        f.R6: "ruling after now",
        f.TERM_Y: "row after now",
    }
    leaked = {stamp: why for stamp, why in absent.items() if stamp in present}
    assert leaked == {}


def test_set_is_a_selection_not_the_prefix() -> None:
    result = derive(f.T1)
    assert result.prefix_row_count == len(f.PREFIX)
    assert 0 < len(result.items) < result.rows_as_of_count < result.prefix_row_count


def test_superseded_not_in_force() -> None:
    rows = {r.stamp for item in derive(f.T1).items for r in item.rows}
    assert f.R3 not in rows
    assert f.R4 in rows


def test_amendment_composes() -> None:
    items = [i for i in derive(f.T1).items if i.kind is K.RULING]
    both = [i for i in items if {r.stamp for r in i.rows} == {f.R1, f.R2}]
    assert len(both) == 1
    item = both[0]
    assert [r.role for r in item.rows] == ["primary", "amender"]
    assert "Use blue." in item.quote
    assert "Use blue, except in docs." in item.quote
    assert f.R1 in item.required_spans
    assert f.R2 in item.required_spans
    assert not [i for i in items if f.R1 in {r.stamp for r in i.rows} and i is not item]


def test_correction_closure() -> None:
    items = [i for i in derive(f.T1).items if i.kind is K.RULING]
    corrected = [i for i in items if f.R5 in {r.stamp for r in i.rows}]
    assert len(corrected) == 1
    item = corrected[0]
    assert [(r.stamp, r.role) for r in item.rows] == [
        (f.R5, "primary"),
        (f.CORR5, "correction"),
    ]
    assert item.rows[1].operator_span == "0:4"
    assert f.CORR5 in item.required_spans


def test_correction_absent_before_it_is_written() -> None:
    earlier = f"{f.D}T10:41:00Z"
    items = [i for i in derive(earlier).items if i.kind is K.RULING]
    corrected = [i for i in items if f.R5 in {r.stamp for r in i.rows}]
    assert [len(i.rows) for i in corrected] == [1]


def test_items_carry_key_quote_spans_conduct_and_stamps() -> None:
    result = derive(f.T1)
    by_kind = {i.kind: i for i in result.items}
    assert all(i.key and i.quote and i.required_spans and i.rows for i in result.items)
    assert by_kind[K.RULING].expected_conduct is EnumExpectedConduct.NOT_ACT_AGAINST
    assert by_kind[K.OPEN_ASK].expected_conduct is EnumExpectedConduct.CARRY_PENDING
    assert len({i.key for i in result.items}) == len(result.items)
    claim = next(
        i for i in result.items if i.kind is K.OPEN_CLAIM and "lane-y" in i.key
    )
    assert claim.required_spans == ("lane-y",)
    draft = next(
        i for i in result.items if i.kind is K.DRAFT_UNSENT and "draft1" in i.key
    )
    assert draft.required_spans == ("/tmp/draft1.md",)
    promise = next(i for i in result.items if i.kind is K.PROMISE_SENT)
    assert promise.required_spans == ("disput", "invoic", "vendor")


def test_every_stamp_is_in_the_prefix() -> None:
    """AC2: each item's row stamp is found in the pinned prefix (the grep -c check)."""
    text = "\n".join(f.PREFIX)
    for now in (f.T1, f.T2, f.T3):
        for item in derive(now).items:
            for row in item.rows:
                assert text.count(row.stamp) >= 1


def test_same_inputs_same_set_and_hash() -> None:
    """AC4."""
    first, second = derive(f.T1), derive(f.T1)
    assert first == second
    assert first.set_hash == second.set_hash
    assert derive(f.T2).set_hash != first.set_hash


def test_hash_ignores_nothing_in_the_set() -> None:
    changed = tuple(row.replace("Use spaces.", "Use two spaces.") for row in f.PREFIX)
    assert derive(f.T1, changed).set_hash != derive(f.T1).set_hash


def test_unparseable_rows_are_counted_not_fatal() -> None:
    noisy = (
        *f.PREFIX,
        "not a ledger row",
        "",
        f"{f.s('12:00:01')} | NOTE | lane=x | y",
    )
    result = derive(f.T1, noisy)
    assert result.refused_row_count == 2
    assert result.items == derive(f.T1).items


def test_lane_family_is_read_from_parent_cells() -> None:
    family = derive(f.T1).lane_family
    assert set(family) == {"orch-a", "lane-old", "lane-x", "lane-y"}


def test_extra_lane_joins_the_family() -> None:
    result = derive(f.T1, extra_lanes=("other-lane",))
    claims = {i.key for i in result.items if i.kind is K.OPEN_CLAIM}
    assert any("other-lane" in key for key in claims)
