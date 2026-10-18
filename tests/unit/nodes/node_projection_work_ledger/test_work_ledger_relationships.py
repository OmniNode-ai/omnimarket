# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ruling relationships and correction closure in the projection fold (OMN-21107, TA.5).

Each fixture folds to its expected open or closed state and links, and the
state is identical under every arrival permutation of its rows.
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import UTC, datetime

import pytest

from omnimarket.enums.enum_work_ledger_entity_kind import (
    EnumWorkLedgerRelation,
)
from omnimarket.handlers.work_ledger_fold import (
    apply_ops,
    close_relationships,
    fold_row,
    links_for_rows,
    target_stamp_of,
)
from omnimarket.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)

pytestmark = pytest.mark.unit

RULING = '2026-10-01T09:00:00Z | RULING | lane=orch | ticket=OMN-1 | kind=decision | "use the lab" | first answer'
SUPERSEDING = '2026-10-01T10:00:00Z | RULING | lane=orch | ticket=OMN-1 | kind=decision | supersedes=2026-10-01T09:00:00Z | "use the pool" | replaces it'
AMENDING = '2026-10-01T10:00:00Z | RULING | lane=orch | ticket=OMN-1 | kind=decision | amends=2026-10-01T09:00:00Z | "and the pool" | widens it'
CONSENT = '2026-10-01T09:05:00Z | OPERATOR-CONSENT | lane=orch | "yes" | APPROVED SCOPE: lab | OUT OF SCOPE: prod | evidence'
CONSENT_EXT = '2026-10-01T11:05:00Z | OPERATOR-CONSENT | lane=orch | extends=2026-10-01T09:05:00Z | "keep going" | APPROVED SCOPE: lab | OUT OF SCOPE: prod | continued'
CORRECTION = "2026-10-01T09:10:00Z | CORRECTION | lane=orch | ticket=OMN-1 | corrects=2026-10-01T09:00:00Z | operator_span=0:11 | the quoted cell holds framing"
HOLD = "2026-10-01T09:00:00Z | HOLD | lane=beta | id=2026-10-01T09:00:00Z-beta | surface=lab-dev | until=2026-10-01T12:00:00Z | reserved"
RELEASE = "2026-10-01T10:00:00Z | RELEASE | lane=beta | re=2026-10-01T09:00:00Z-beta | surface=lab-dev | result=PASS | restored=yes | done"
CLAIM = "2026-10-01T09:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work"
TERMINAL = (
    "2026-10-01T09:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | done"
)
RELEASE_CLAIM = (
    "2026-10-01T09:30:00Z | RELEASE | lane=alpha | re=2026-10-01T09:00:00Z | gave it up"
)
MSG = "2026-10-01T09:15:00Z | MSG | from=alpha | to=beta | id=2026-10-01T09:15:00Z-alpha | look"
ACK = "2026-10-01T09:16:00Z | ACK | from=beta | to=alpha | id=2026-10-01T09:16:00Z-beta | re=2026-10-01T09:15:00Z-alpha"

RULING_KEY = "ruling:2026-10-01T09:00:00Z:orch"
CONSENT_KEY = "consent:2026-10-01T09:05:00Z:orch"
HOLD_KEY = "hold:2026-10-01T09:00:00Z-beta"

# The real D1 pair, quoted whole from the rolling ledger, each pinned by the
# sha256 its plan excerpt records for the row line.
D1_RULING = '2026-10-02T11:25:17Z | RULING | lane=claude-mods-plan | ticket=OMN-18531 | kind=decision | verbatim=encoded | "D1 (mod file shape / ratchet exception), plan beta/plans/2026-10-01-claude-code-context-and-delegation-adapter-plan.md, decision D1. Operator answer, 2026-10-02, in-session: %22mod file for what? nothing here should be non-canonical%22"'
D1_CORRECTION = "2026-10-02T11:30:10Z | CORRECTION | lane=claude-mods-plan | ticket=OMN-18531 | corrects=2026-10-02T11:25:17Z | RULING row 2026-10-02T11:25:17Z (decision D1) quotes caller framing inside its quoted cell; the operator's words are only the span after \"in-session:\", that is: mod file for what? nothing here should be non-canonical. The framing text before it is context, not the operator's words."
D1_SHA256 = {
    D1_RULING: "479f00b9724b466704de70a3fca18c7c64b87da006479da815633fd3027c546a",
    D1_CORRECTION: "fa71fd2a59b8fe8fa4a0707e08c8d1863417eba6be5fa5be25ab92eaf51dfdd8",
}


def _close(
    rows: tuple[str, ...], as_of: datetime | None = None
) -> dict[str, dict[str, object]]:
    state: dict[str, dict[str, object]] = {}
    links = []
    for row in rows:
        result = fold_row(ModelWorkLedgerFoldRequest(raw_row=row))
        apply_ops(state, result.ops)
        links.extend(result.links)
    closure, _unresolved = close_relationships(state, links, as_of=as_of)
    return closure


def _row_id(row: str) -> str:
    return fold_row(ModelWorkLedgerFoldRequest(raw_row=row)).row.row_id


AT_11 = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
AT_13 = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)

# (name, rows, as_of, entity, is_open, relation field, linking row or None)
FIXTURES: list[tuple[str, tuple[str, ...], datetime | None, str, bool, str, str | None]] = [
    ("superseded_ruling", (RULING, SUPERSEDING), None, RULING_KEY, False, "superseded_by", SUPERSEDING),
    ("amended_ruling", (RULING, AMENDING), None, RULING_KEY, True, "amended_by", AMENDING),
    ("consent_extended", (CONSENT, CONSENT_EXT), None, CONSENT_KEY, True, "extended_by", CONSENT_EXT),
    ("corrected_ruling", (RULING, CORRECTION), None, RULING_KEY, True, "corrected_by", CORRECTION),
    ("released_hold", (HOLD, RELEASE), None, HOLD_KEY, False, "corrected_by", None),
    ("hold_before_until", (HOLD,), AT_11, HOLD_KEY, True, "corrected_by", None),
    ("hold_past_until", (HOLD,), AT_13, HOLD_KEY, False, "corrected_by", None),
    ("claim_closed_by_terminal", (CLAIM, TERMINAL), None, "claim:alpha", False, "corrected_by", None),
    ("claim_closed_by_release", (CLAIM, RELEASE_CLAIM), None, "claim:alpha", False, "corrected_by", None),
    ("acked_msg", (MSG, ACK), None, "msg:2026-10-01T09:15:00Z-alpha", False, "corrected_by", None),
]  # fmt: skip


@pytest.mark.parametrize(
    ("rows", "as_of", "key", "is_open", "field", "linker"),
    [f[1:] for f in FIXTURES],
    ids=[f[0] for f in FIXTURES],
)
def test_each_fixture_folds_to_its_state_and_links_under_every_permutation(
    rows: tuple[str, ...],
    as_of: datetime | None,
    key: str,
    is_open: bool,
    field: str,
    linker: str | None,
) -> None:
    expected_links = (_row_id(linker),) if linker else ()
    states = [_close(perm, as_of) for perm in itertools.permutations(rows)]
    for state in states:
        assert state[key]["is_open"] is is_open
        assert state[key][field] == expected_links
    assert all(state == states[0] for state in states)


def test_amends_and_extends_link_without_closing_and_supersedes_only_closes_its_target() -> (
    None
):
    state = _close((RULING, CONSENT, AMENDING, CONSENT_EXT))
    assert state[RULING_KEY]["is_open"] is True
    assert state[CONSENT_KEY]["is_open"] is True
    later = _close((RULING, CONSENT, SUPERSEDING))
    assert later["ruling:2026-10-01T10:00:00Z:orch"]["is_open"] is True
    assert later[CONSENT_KEY]["is_open"] is True
    assert later[RULING_KEY]["superseded_by"] == (_row_id(SUPERSEDING),)


def test_a_correction_folds_to_a_link_with_its_operator_span() -> None:
    result = fold_row(ModelWorkLedgerFoldRequest(raw_row=CORRECTION))
    assert result.ops == ()
    (link,) = result.links
    assert link.relation is EnumWorkLedgerRelation.CORRECTS
    assert link.target_ref == "2026-10-01T09:00:00Z"
    assert link.target_stamp == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert link.operator_span == "0:11"
    state = _close((RULING, CORRECTION))
    assert state[RULING_KEY]["operator_spans"] == ((_row_id(CORRECTION), "0:11"),)


def test_a_corrected_terminal_lists_the_correction_on_the_claim_it_closed() -> None:
    correction = "2026-10-01T09:40:00Z | CORRECTION | lane=alpha | corrects=TERMINAL 2026-10-01T09:30:00Z | wrong outcome"
    state = _close((CLAIM, TERMINAL, correction))
    assert state["claim:alpha"]["corrected_by"] == (_row_id(correction),)


def test_the_real_d1_pair_folds_to_one_open_ruling_listing_its_correction() -> None:
    for row, digest in D1_SHA256.items():
        assert hashlib.sha256(row.encode()).hexdigest() == digest
    for rows in itertools.permutations((D1_RULING, D1_CORRECTION)):
        state = _close(rows)
        rulings = {k: v for k, v in state.items() if v["kind"] == "ruling"}
        assert list(rulings) == ["ruling:2026-10-02T11:25:17Z:claude-mods-plan"]
        (ruling,) = rulings.values()
        assert ruling["is_open"] is True
        assert ruling["corrected_by"] == (_row_id(D1_CORRECTION),)


def test_a_target_naming_no_folded_entity_is_reported_unresolved() -> None:
    row = (
        "2026-10-01T09:10:00Z | CORRECTION | lane=orch | corrects=ledger:1234 | by line"
    )
    result = fold_row(ModelWorkLedgerFoldRequest(raw_row=row))
    assert result.links[0].target_stamp is None
    _closure, unresolved = close_relationships({}, result.links)
    assert unresolved == result.links


@pytest.mark.parametrize(
    ("ref", "stamp"),
    [
        ("2026-10-01T09:00:00Z", "2026-10-01T09:00:00Z"),
        ("RULING 2026-10-01T09:00:00Z", "2026-10-01T09:00:00Z"),
        ("CLAIM@2026-10-01T09:00:00Z", "2026-10-01T09:00:00Z"),
        ("2026-10-01T09:00:00Z (ledger:12)", "2026-10-01T09:00:00Z"),
        ("2026-10-01T09:00:00Z-beta", None),
        ("ledger:1234", None),
    ],
)
def test_the_target_stamp_is_a_row_timestamp_never_a_hold_or_message_id(
    ref: str, stamp: str | None
) -> None:
    got = target_stamp_of(ref)
    assert (got.strftime("%Y-%m-%dT%H:%M:%SZ") if got else None) == stamp


def test_links_for_rows_rederives_every_link_and_skips_noncanonical_rows() -> None:
    links = links_for_rows([RULING, SUPERSEDING, CORRECTION, "not a ledger row"])
    assert sorted((lk.relation.value, lk.source_row_id) for lk in links) == sorted(
        [("supersedes", _row_id(SUPERSEDING)), ("corrects", _row_id(CORRECTION))]
    )
