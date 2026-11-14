# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the landing ledger and the landing decision: the review gate (OMN-24477).

A review is recorded as ledger rows: a STATUS row marks a PR as new work, and a reviewer lane's
STATUS row records a verdict for one head. ``derive_ledger_facts`` folds those rows into each
PR's ``review`` fact under the short key ``repo#n``; the caller carries the fact onto the PR's
facts under the full key, and the decision asks the review gate before it merges the head. The
tests pipe real rows through the real deriver into the real decision, so a change on either side
of the seam fails here: a verdict from a lane that is not an accepted reviewer, a row that is
not a STATUS, a head that is not forty hex characters or an engine that is not accepted reads
exactly as no verdict, and a verdict row alone marks no PR as new work.

Two injected breaks show the assertions can fail for real: a deriver that accepts a verdict from
any lane, and one that stores a verdict under the PR's current head whatever head the row names.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingLandSkipReason,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_review import (
    EnumReviewGateVerdict,
    ModelLandingReviewFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.review_gate import (
    SKIP_FOR_REVIEW,
)
from omnimarket.nodes.node_pr_landing_ledger_facts_compute.handlers import (
    handler_pr_landing_ledger_facts as ledger_module,
)
from omnimarket.nodes.node_pr_landing_ledger_facts_compute.handlers.handler_pr_landing_ledger_facts import (
    derive_ledger_facts,
)
from omnimarket.nodes.node_pr_landing_ledger_facts_compute.models.model_landing_ledger_facts import (
    ModelLandingLedgerFacts,
    ModelLandingLedgerRow,
    ModelLandingLedgerRows,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 13, 0, 0, tzinfo=UTC)
PR = "OmniNode-ai/omnimarket#9001"
SHORT = "omnimarket#9001"  # the ledger's key of the same PR
HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
OLD_HEAD = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
NODE = "node_pr_landing_decision_compute"
PIECE = 9
REVIEWER = "orchestrator-31d9"


def _row(ts: str, rtype: str, lane: str, text: str) -> ModelLandingLedgerRow:
    return ModelLandingLedgerRow(ts=ts, rtype=rtype, lane=lane, text=text)


def _mark(ts: str = "2026-11-07T12:45:00Z") -> ModelLandingLedgerRow:
    """The STATUS row that marks a PR as new work."""
    return _row(
        ts,
        "STATUS",
        "ticket-9001",
        f"{ts} | STATUS | lane=ticket-9001 | pr={SHORT} | work_kind=new_work",
    )


def _verdict(
    head: str = HEAD,
    lane: str = REVIEWER,
    verdict: str = "CLEAN",
    engine: str = "fable",
    rtype: str = "STATUS",
    ts: str = "2026-11-07T12:50:00Z",
) -> ModelLandingLedgerRow:
    """A ledger row a reviewer lane recorded a review verdict on."""
    return _row(
        ts,
        rtype,
        lane,
        f"{ts} | {rtype} | lane={lane} | pr={SHORT} | head={head}"
        f" | review={engine} | verdict={verdict}",
    )


def _review(rows: tuple[ModelLandingLedgerRow, ...], **request: Any) -> dict[str, Any]:
    """The ledger's review fact of the PR, as the caller carries it onto the PR's facts."""
    ledger = derive_ledger_facts(ModelLandingLedgerRows(now=NOW, rows=rows, **request))
    entry = ledger.review.get(SHORT)
    return entry.model_dump(mode="json") if entry is not None else {"new_work": False}


def _pipe(rows: tuple[ModelLandingLedgerRow, ...], **request: Any) -> ModelLandingFacts:
    """The seam: rows through the ledger compute, the review fact onto the PR's facts."""
    row: dict[str, Any] = {
        "pr": PR,
        "head_sha": HEAD,
        "state": "open",
        "ci": "green",
        "merge_state": "clean",
        "created_at": "2026-11-07T11:00:00Z",
        "review": _review(rows, **request),
    }
    return ModelLandingFacts.model_validate(
        {
            "tick": 5,
            "observed_at": NOW.isoformat(),
            "prs": [row],
            "state": {"last_tick": 4, "records": []},
        }
    )


def _merges(facts: ModelLandingFacts) -> list[str]:
    return [
        a.head_sha or ""
        for a in decide_landing(facts).actions
        if a.kind is EnumLandingActionKind.MERGE and a.subject == PR
    ]


def _skip(facts: ModelLandingFacts) -> EnumLandingLandSkipReason | None:
    rows = [s for s in decide_landing(facts).land_skips if s.pr == PR]
    return rows[0].reason if rows else None


def _expect(
    rows: tuple[ModelLandingLedgerRow, ...], skip: EnumLandingLandSkipReason
) -> None:
    """The exact expectation of one held head, and the error chain the decision node emits."""
    facts = _pipe(rows)
    assert _merges(facts) == []
    assert _skip(facts) is skip
    cid = f"review-gate-seam-{skip.value}"
    sink = InMemoryEmissionSink()
    chain = ChainEmitter(
        sink, node=NODE, piece=PIECE, correlation_id=cid, backend="in_memory"
    )
    chain.start()
    chain.failure(skip.value)
    assert [(e.phase, e.node, e.piece) for e in sink.events] == [
        ("start", NODE, PIECE),
        ("failure", NODE, PIECE),
    ]
    assert {e.correlation_id for e in sink.events} == {cid}
    assert sink.events[1].failure_code == skip.value


# ---------------------------------------------------------------------------------------------- golden


def test_a_marked_new_work_head_with_a_clean_verdict_on_it_merges() -> None:
    """The golden path end to end: the rows merge the head, and the node emits its chain."""
    facts = _pipe((_mark(), _verdict()))
    assert _merges(facts) == [HEAD]
    assert _skip(facts) is None
    cid = "review-gate-seam-golden"
    sink = InMemoryEmissionSink()
    chain = ChainEmitter(
        sink, node=NODE, piece=PIECE, correlation_id=cid, backend="in_memory"
    )
    chain.start()
    chain.success({"merged": 1})
    assert [(e.phase, e.node, e.piece) for e in sink.events] == [
        ("start", NODE, PIECE),
        ("success", NODE, PIECE),
    ]
    assert {e.correlation_id for e in sink.events} == {cid}
    assert sink.events[1].counters == {"merged": 1}


def test_a_verdict_row_without_a_new_work_mark_merges_the_pr_as_not_new_work() -> None:
    """A verdict marks nothing: only a work_kind=new_work row puts the PR under the gate."""
    facts = _pipe((_verdict(),))
    assert _merges(facts) == [HEAD]
    assert _skip(facts) is None


# --------------------------------------------------------------------------------------------- error


def test_a_marked_head_with_no_verdict_row_is_held_review_missing() -> None:
    _expect((_mark(),), EnumLandingLandSkipReason.REVIEW_MISSING)


def test_a_marked_head_with_changes_needed_on_it_is_held_review_changes_needed() -> (
    None
):
    _expect(
        (_mark(), _verdict(verdict="CHANGES-NEEDED")),
        EnumLandingLandSkipReason.REVIEW_CHANGES_NEEDED,
    )


def test_a_clean_verdict_only_for_an_older_head_is_held_review_superseded() -> None:
    _expect(
        (_mark(), _verdict(head=OLD_HEAD)),
        EnumLandingLandSkipReason.REVIEW_SUPERSEDED,
    )


def test_a_verdict_from_a_non_reviewer_lane_is_held_review_missing() -> None:
    """dash-valkey-6803 is a work lane, not an accepted reviewer: its verdict counts as none."""
    _expect(
        (_mark(), _verdict(lane="dash-valkey-6803")),
        EnumLandingLandSkipReason.REVIEW_MISSING,
    )


def test_a_verdict_row_that_is_not_a_status_row_is_held_review_missing() -> None:
    _expect((_mark(), _verdict(rtype="MSG")), EnumLandingLandSkipReason.REVIEW_MISSING)


def test_a_verdict_row_with_a_ten_character_head_is_held_review_missing() -> None:
    _expect(
        (_mark(), _verdict(head=HEAD[:10])),
        EnumLandingLandSkipReason.REVIEW_MISSING,
    )


def test_a_verdict_row_of_an_unaccepted_engine_is_held_review_missing() -> None:
    _expect(
        (_mark(), _verdict(engine="codex")),
        EnumLandingLandSkipReason.REVIEW_MISSING,
    )


# ------------------------------------------------------------------------------------------- overrides


def test_the_requests_lane_prefixes_decide_which_lane_may_review() -> None:
    override = {"review_lane_prefixes": ("reviewer-",)}
    assert _merges(_pipe((_mark(), _verdict(lane="reviewer-1")), **override)) == [HEAD]
    other = _pipe((_mark(), _verdict()), **override)
    assert _merges(other) == []
    assert _skip(other) is EnumLandingLandSkipReason.REVIEW_MISSING


# --------------------------------------------------------------------------------------------- shape


def test_the_ledgers_review_fact_is_a_dict_of_the_reviews_model() -> None:
    annotation = ModelLandingLedgerFacts.model_fields["review"].annotation
    assert annotation == dict[str, ModelLandingReviewFacts]


def test_the_prs_review_field_accepts_exactly_the_derived_review() -> None:
    minimal: dict[str, Any] = {
        "pr": PR,
        "head_sha": HEAD,
        "state": "open",
        "ci": "green",
        "created_at": "2026-11-07T11:00:00Z",
    }
    derived = _review((_mark(), _verdict()))
    ModelLandingPrFacts.model_validate({**minimal, "review": derived})
    with pytest.raises(ValidationError):
        ModelLandingPrFacts.model_validate(
            {**minimal, "review": {**derived, "verdict_count": 0}}
        )


def test_the_review_skip_reasons_are_exactly_the_four_named_readings() -> None:
    assert {
        reason.value
        for reason in EnumLandingLandSkipReason
        if reason.value.startswith("review_")
    } == {
        "review_missing",
        "review_changes_needed",
        "review_superseded",
        "review_unread",
    }


def test_the_reader_holds_every_reading_except_pass() -> None:
    expected = set(EnumReviewGateVerdict) - {EnumReviewGateVerdict.PASS}
    assert set(SKIP_FOR_REVIEW) == expected


# ------------------------------------------------------------------------------------------- controls


def test_break_a_deriver_that_accepts_any_lane_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = (_mark(), _verdict(lane="dash-valkey-6803"))
    _expect(
        rows, EnumLandingLandSkipReason.REVIEW_MISSING
    )  # the real deriver refuses it

    real = ledger_module.review_facts

    def accepts_any_lane(
        ledger_rows: tuple[ModelLandingLedgerRow, ...],
        at: datetime,
        prefixes: tuple[str, ...],
        engines: tuple[str, ...],
    ) -> dict[str, ModelLandingReviewFacts]:
        return real(ledger_rows, at, ("",), engines)  # every lane starts with ""

    monkeypatch.setattr(ledger_module, "review_facts", accepts_any_lane)
    with pytest.raises(AssertionError):
        _expect(rows, EnumLandingLandSkipReason.REVIEW_MISSING)


def test_break_a_deriver_that_stores_any_head_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = (_mark(), _verdict(head=OLD_HEAD))
    _expect(
        rows, EnumLandingLandSkipReason.REVIEW_SUPERSEDED
    )  # the real deriver keeps it

    real = ledger_module.review_facts

    def stores_any_head(
        ledger_rows: tuple[ModelLandingLedgerRow, ...],
        at: datetime,
        prefixes: tuple[str, ...],
        engines: tuple[str, ...],
    ) -> dict[str, ModelLandingReviewFacts]:
        return {
            pr: fact.model_copy(
                update={
                    "verdicts": tuple(
                        row.model_copy(update={"head_sha": HEAD})
                        for row in fact.verdicts
                    )
                }
            )
            for pr, fact in real(ledger_rows, at, prefixes, engines).items()
        }

    monkeypatch.setattr(ledger_module, "review_facts", stores_any_head)
    with pytest.raises(AssertionError):
        _expect(rows, EnumLandingLandSkipReason.REVIEW_SUPERSEDED)
