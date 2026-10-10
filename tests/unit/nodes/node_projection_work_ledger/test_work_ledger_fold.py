# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-first tests for the pure work-ledger fold (OMN-19513, rule 7a class one)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from omnimarket.events.model_ledger_row_event import (
    ModelLedgerRowEventBase,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_fold import (
    WorkLedgerFoldError,
    apply_ops,
    fold_row,
)
from omnimarket.nodes.node_projection_work_ledger.models.enum_work_ledger_entity_kind import (
    EnumWorkLedgerEntityKind,
    EnumWorkLedgerStateOp,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)

pytestmark = pytest.mark.unit

CLAIM_A = "2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | repo=omnimarket | est ~1 lane-hours; displaces x; (OMN-1) | work"
TERM_A = (
    "2026-09-28T10:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | done"
)
CLAIM_A2 = "2026-09-28T11:00:00Z | CLAIM | lane=alpha | ticket=OMN-2 | est ~1 lane-hours; displaces x; (OMN-2) | again"
HOLD = "2026-09-28T10:10:00Z | HOLD | lane=beta | id=2026-09-28T10:10:00Z-beta | surface=lab-dev | until=2026-09-28T12:00:00Z | reserved"
RELEASE = "2026-09-28T11:00:00Z | RELEASE | lane=beta | re=2026-09-28T10:10:00Z-beta | surface=lab-dev | result=PASS | restored=yes | done"
MSG = "2026-09-28T10:15:00Z | MSG | from=alpha | to=beta,gamma | id=2026-09-28T10:15:00Z-alpha | look"
ACK = "2026-09-28T10:16:00Z | ACK | from=beta | to=alpha | id=2026-09-28T10:16:00Z-beta | re=2026-09-28T10:15:00Z-alpha"
RULING = '2026-09-28T09:00:00Z | RULING | lane=orch | ticket=OMN-1 | "go" | text'
CONSENT = '2026-09-28T09:01:00Z | OPERATOR-CONSENT | lane=orch | "yes" | APPROVED SCOPE: lab | OUT OF SCOPE: prod | evidence'
STATUS = "2026-09-28T10:05:00Z | STATUS | lane=alpha | ticket=OMN-1 | progress"

ALL_ROWS = [CLAIM_A, TERM_A, CLAIM_A2, HOLD, RELEASE, MSG, ACK, RULING, CONSENT, STATUS]


def _fold_all(rows: list[str]) -> dict[str, dict[str, object]]:
    state: dict[str, dict[str, object]] = {}
    for row in rows:
        apply_ops(state, fold_row(ModelWorkLedgerFoldRequest(raw_row=row)).ops)
    return state


def _open(
    state: dict[str, dict[str, object]], kind: EnumWorkLedgerEntityKind
) -> set[str]:
    return {k for k, v in state.items() if v["kind"] == kind.value and v["is_open"]}


def test_a_claim_opens_and_its_lanes_terminal_closes_it() -> None:
    assert _open(_fold_all([CLAIM_A]), EnumWorkLedgerEntityKind.CLAIM) == {
        "claim:alpha"
    }
    assert _open(_fold_all([CLAIM_A, TERM_A]), EnumWorkLedgerEntityKind.CLAIM) == set()


def test_a_later_claim_by_the_same_lane_reopens() -> None:
    state = _fold_all([CLAIM_A, TERM_A, CLAIM_A2])
    assert _open(state, EnumWorkLedgerEntityKind.CLAIM) == {"claim:alpha"}
    assert state["claim:alpha"]["ticket"] == "OMN-2"


def test_a_hold_is_live_with_its_scope_until_a_release_names_its_id() -> None:
    state = _fold_all([HOLD])
    assert _open(state, EnumWorkLedgerEntityKind.HOLD) == {
        "hold:2026-09-28T10:10:00Z-beta"
    }
    hold = state["hold:2026-09-28T10:10:00Z-beta"]
    assert hold["scope_surface"] == "lab-dev"
    assert hold["until_at"] == datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    assert _open(_fold_all([HOLD, RELEASE]), EnumWorkLedgerEntityKind.HOLD) == set()


def test_a_message_is_unanswered_until_an_ack_names_its_id() -> None:
    assert _open(_fold_all([MSG]), EnumWorkLedgerEntityKind.MSG) == {
        "msg:2026-09-28T10:15:00Z-alpha"
    }
    assert _open(_fold_all([MSG, ACK]), EnumWorkLedgerEntityKind.MSG) == set()


def test_rulings_and_consents_are_entities_that_stay_open() -> None:
    state = _fold_all([RULING, CONSENT])
    assert _open(state, EnumWorkLedgerEntityKind.RULING) == {
        "ruling:2026-09-28T09:00:00Z:orch"
    }
    consent = state["consent:2026-09-28T09:01:00Z:orch"]
    assert consent["detail"] == "APPROVED SCOPE: lab | OUT OF SCOPE: prod"


def test_a_status_row_is_logged_and_opens_no_entity() -> None:
    result = fold_row(ModelWorkLedgerFoldRequest(raw_row=STATUS))
    assert result.ops == ()
    assert result.row.row_type == "STATUS"


def test_every_row_is_logged_with_its_hash_and_stamp() -> None:
    result = fold_row(ModelWorkLedgerFoldRequest(raw_row=CLAIM_A))
    assert result.row.row_ts == datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
    assert result.row.raw_row == CLAIM_A
    assert len(result.row.row_id) == 64


def test_redelivery_and_reordering_leave_the_same_state() -> None:
    baseline = _fold_all(ALL_ROWS)
    # Redelivery: every row twice.
    assert _fold_all(ALL_ROWS + ALL_ROWS) == baseline
    # Reordering: a sample of permutations, including the full reverse.
    assert _fold_all(list(reversed(ALL_ROWS))) == baseline
    for perm in itertools.islice(itertools.permutations(ALL_ROWS), 0, 3000, 97):
        assert _fold_all(list(perm)) == baseline


def test_a_release_before_its_hold_still_closes_it() -> None:
    assert _open(_fold_all([RELEASE, HOLD]), EnumWorkLedgerEntityKind.HOLD) == set()


def test_an_ack_before_its_message_still_answers_it() -> None:
    assert _open(_fold_all([ACK, MSG]), EnumWorkLedgerEntityKind.MSG) == set()


def test_an_unparsable_row_is_refused_not_folded() -> None:
    with pytest.raises(WorkLedgerFoldError):
        fold_row(
            ModelWorkLedgerFoldRequest(
                raw_row="2026-09-28T10:00:00Z | NOTE | lane=x | hi"
            )
        )


def test_the_fold_carries_the_extra_fields_the_bus_adds() -> None:
    request = ModelWorkLedgerFoldRequest.model_validate(
        {"raw_row": CLAIM_A, "emitted_at": "x", "entity_id": "y", "lane": "clobbered"}
    )
    assert (
        fold_row(request).ops[0].lane == "alpha"
    )  # raw_row is truth, not payload.lane


def test_ops_enum_values() -> None:
    assert {o.value for o in EnumWorkLedgerStateOp} == {"open", "close"}
    assert issubclass(ModelLedgerRowEventBase, object)


@pytest.mark.parametrize("ledger_seq", [None, 1, 42])
def test_ledger_seq_fold_request_passes_judge_sequence(ledger_seq: int | None) -> None:
    request = ModelWorkLedgerFoldRequest(raw_row=CLAIM_A, ledger_seq=ledger_seq)
    assert request.ledger_seq == ledger_seq
    assert fold_row(request).row.ledger_seq == ledger_seq


def test_ledger_seq_absent_is_none() -> None:
    request = ModelWorkLedgerFoldRequest(raw_row=CLAIM_A)
    assert request.ledger_seq is None
    assert fold_row(request).row.ledger_seq is None


@pytest.mark.parametrize("ledger_seq", [0, -1])
def test_ledger_seq_nonpositive_is_rejected(ledger_seq: int) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelWorkLedgerFoldRequest(raw_row=CLAIM_A, ledger_seq=ledger_seq)
