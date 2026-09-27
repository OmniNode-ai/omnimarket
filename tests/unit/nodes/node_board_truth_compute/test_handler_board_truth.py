# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Red-first tests for the board-truth projection compute.

Every derivation edge in OMN-16731 is asserted here BEFORE the handler exists.
The handler is a pure function: facts in, derived state + evidence out. No I/O,
no clock read — the evaluation instant is an input field.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16731: this node
    - OMN-16106: owns the Done edge this node refuses to touch
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from omnimarket.enums.enum_board_reconcile_action import EnumBoardReconcileAction
from omnimarket.enums.enum_derived_board_state import EnumDerivedBoardState
from omnimarket.enums.enum_pr_state import EnumPrState
from omnimarket.events.board_truth import (
    ModelBoardFactBundle,
    ModelBoardTruthInput,
    ModelBranchFact,
    ModelLedgerClaimFact,
    ModelLinearStateFact,
    ModelPrFact,
)
from omnimarket.nodes.node_board_truth_compute.handlers.handler_board_truth import (
    HandlerBoardTruth,
)

_NOW = datetime(2026, 8, 27, 11, 0, 0, tzinfo=UTC)
_CORRELATION = UUID("00000000-0000-4000-8000-000000000001")
_STALENESS_DAYS = 3


def _state(
    name: str,
    state_type: str = "started",
    *,
    set_by_automation: bool | None = True,
    age_days: int = 1,
) -> ModelLinearStateFact:
    return ModelLinearStateFact(
        state_name=name,
        state_type=state_type,
        set_at=_NOW - timedelta(days=age_days),
        set_by_automation=set_by_automation,
    )


def _bundle(
    ticket: str = "OMN-00001",
    *,
    current_state: ModelLinearStateFact | None = None,
    ledger_claims: tuple[ModelLedgerClaimFact, ...] = (),
    pull_requests: tuple[ModelPrFact, ...] = (),
    branches: tuple[ModelBranchFact, ...] = (),
    facts_complete: bool = True,
) -> ModelBoardFactBundle:
    return ModelBoardFactBundle(
        ticket=ticket,
        current_state=current_state
        if current_state is not None
        else _state("Backlog", "backlog"),
        ledger_claims=ledger_claims,
        pull_requests=pull_requests,
        branches=branches,
        facts_complete=facts_complete,
    )


def _open_pr(*, draft: bool = False, age_days: int = 1) -> ModelPrFact:
    return ModelPrFact(
        repo="omnimarket",
        number=1,
        state=EnumPrState.OPEN,
        draft=draft,
        updated_at=_NOW - timedelta(days=age_days),
    )


def _merged_pr(age_days: int = 1) -> ModelPrFact:
    return ModelPrFact(
        repo="omnimarket",
        number=2,
        state=EnumPrState.MERGED,
        draft=False,
        updated_at=_NOW - timedelta(days=age_days),
    )


def _live_claim(age_days: int = 1) -> ModelLedgerClaimFact:
    return ModelLedgerClaimFact(
        lane="some-lane",
        claimed_at=_NOW - timedelta(days=age_days),
        terminal_at=None,
    )


def _terminal_claim(age_days: int = 5) -> ModelLedgerClaimFact:
    return ModelLedgerClaimFact(
        lane="some-lane",
        claimed_at=_NOW - timedelta(days=age_days + 1),
        terminal_at=_NOW - timedelta(days=age_days),
    )


def _commits(count: int = 3, age_days: int = 1) -> ModelBranchFact:
    return ModelBranchFact(
        repo="omnimarket",
        branch="jonah/omn-00001-thing",
        commit_count=count,
        last_commit_at=_NOW - timedelta(days=age_days),
    )


def _run(*bundles: ModelBoardFactBundle) -> tuple:
    handler = HandlerBoardTruth()
    output = handler.handle(
        ModelBoardTruthInput(
            correlation_id=_CORRELATION,
            evaluated_at=_NOW,
            staleness_days=_STALENESS_DAYS,
            tickets=bundles,
        )
    )
    return output.rows


def _one(bundle: ModelBoardFactBundle):
    rows = _run(bundle)
    assert len(rows) == 1
    return rows[0]


# --- IN_REVIEW edge --------------------------------------------------------


@pytest.mark.unit
def test_open_non_draft_pr_entails_in_review() -> None:
    row = _one(_bundle(pull_requests=(_open_pr(),)))
    assert row.derived_state is EnumDerivedBoardState.IN_REVIEW
    assert row.evidence, "a derivation must cite the facts that entailed it"


@pytest.mark.unit
def test_draft_pr_does_not_entail_in_review() -> None:
    row = _one(_bundle(pull_requests=(_open_pr(draft=True),)))
    assert row.derived_state is EnumDerivedBoardState.IN_PROGRESS


@pytest.mark.unit
def test_in_review_outranks_in_progress_by_declared_precedence() -> None:
    row = _one(_bundle(pull_requests=(_open_pr(),), ledger_claims=(_live_claim(),)))
    assert row.derived_state is EnumDerivedBoardState.IN_REVIEW


# --- IN_PROGRESS edge ------------------------------------------------------


@pytest.mark.unit
def test_unterminated_ledger_claim_entails_in_progress() -> None:
    row = _one(_bundle(ledger_claims=(_live_claim(age_days=90),)))
    assert row.derived_state is EnumDerivedBoardState.IN_PROGRESS


@pytest.mark.unit
def test_recent_branch_commits_entail_in_progress() -> None:
    row = _one(_bundle(branches=(_commits(age_days=1),)))
    assert row.derived_state is EnumDerivedBoardState.IN_PROGRESS


# --- BACKLOG reaper edge ---------------------------------------------------


@pytest.mark.unit
def test_terminal_claim_no_pr_no_commits_entails_backlog() -> None:
    row = _one(
        _bundle(
            current_state=_state("In Progress"),
            ledger_claims=(_terminal_claim(),),
        )
    )
    assert row.derived_state is EnumDerivedBoardState.BACKLOG
    assert row.action is EnumBoardReconcileAction.FLIP


@pytest.mark.unit
def test_never_claimed_never_worked_in_progress_ticket_is_reaped() -> None:
    """The OMN-15579 / OMN-16675 class: In Progress with zero underlying facts."""
    row = _one(_bundle(current_state=_state("In Progress")))
    assert row.derived_state is EnumDerivedBoardState.BACKLOG
    assert row.action is EnumBoardReconcileAction.FLIP


@pytest.mark.unit
def test_stale_commits_outside_window_do_not_hold_in_progress() -> None:
    row = _one(
        _bundle(
            current_state=_state("In Progress"),
            branches=(_commits(age_days=_STALENESS_DAYS + 5),),
        )
    )
    assert row.derived_state is EnumDerivedBoardState.BACKLOG


@pytest.mark.unit
def test_staleness_is_never_derived_from_the_linear_updated_timestamp() -> None:
    """A bulk-triage touch must not read as freshness (OMN-15269 item 4)."""
    row = _one(
        _bundle(
            current_state=_state("In Progress", age_days=0),
            ledger_claims=(_terminal_claim(),),
        )
    )
    assert row.derived_state is EnumDerivedBoardState.BACKLOG


# --- Done edge is refused, not taken ---------------------------------------


@pytest.mark.unit
def test_merged_pr_defers_to_dod_verify_and_never_flips() -> None:
    row = _one(
        _bundle(current_state=_state("In Review"), pull_requests=(_merged_pr(),))
    )
    assert row.derived_state is EnumDerivedBoardState.DEFER_TO_DOD_VERIFY
    assert row.action is EnumBoardReconcileAction.DISCREPANCY


@pytest.mark.unit
def test_already_completed_ticket_is_left_alone() -> None:
    row = _one(
        _bundle(
            current_state=_state("Done", "completed"),
            pull_requests=(_merged_pr(),),
        )
    )
    assert row.action is EnumBoardReconcileAction.NO_CHANGE


@pytest.mark.unit
def test_no_row_may_ever_carry_a_flip_to_a_completed_state() -> None:
    rows = _run(
        _bundle("OMN-A", pull_requests=(_merged_pr(),)),
        _bundle(
            "OMN-B", current_state=_state("In Review"), pull_requests=(_merged_pr(),)
        ),
        _bundle("OMN-C", current_state=_state("Done", "completed")),
    )
    flips = [r for r in rows if r.action is EnumBoardReconcileAction.FLIP]
    assert all(
        r.derived_state is not EnumDerivedBoardState.DEFER_TO_DOD_VERIFY for r in flips
    )


# --- Fail-closed rules -----------------------------------------------------


@pytest.mark.unit
def test_incomplete_facts_are_ambiguous_never_a_flip() -> None:
    row = _one(_bundle(current_state=_state("In Progress"), facts_complete=False))
    assert row.derived_state is EnumDerivedBoardState.AMBIGUOUS
    assert row.action is EnumBoardReconcileAction.DISCREPANCY
    assert row.discrepancy_reason


@pytest.mark.unit
def test_two_edges_without_precedence_are_ambiguous() -> None:
    """Merged PR (completion) plus fresh commits (active work) contradict."""
    row = _one(
        _bundle(
            current_state=_state("In Review"),
            pull_requests=(_merged_pr(),),
            branches=(_commits(age_days=0),),
        )
    )
    assert row.derived_state is EnumDerivedBoardState.AMBIGUOUS
    assert row.action is EnumBoardReconcileAction.DISCREPANCY


@pytest.mark.unit
def test_derivation_matching_current_state_is_a_no_op() -> None:
    row = _one(_bundle(current_state=_state("In Review"), pull_requests=(_open_pr(),)))
    assert row.action is EnumBoardReconcileAction.NO_CHANGE


@pytest.mark.unit
def test_every_flip_cites_evidence() -> None:
    rows = _run(
        _bundle("OMN-A", current_state=_state("In Progress")),
        _bundle(
            "OMN-B",
            current_state=_state("Backlog", "backlog"),
            pull_requests=(_open_pr(),),
        ),
    )
    for row in rows:
        if row.action is EnumBoardReconcileAction.FLIP:
            assert row.evidence, f"{row.ticket} flips with no citable fact"


# --- Human-set guard -------------------------------------------------------


@pytest.mark.unit
def test_human_set_state_flags_a_flip_for_confirmation() -> None:
    row = _one(_bundle(current_state=_state("In Progress", set_by_automation=False)))
    assert row.action is EnumBoardReconcileAction.FLIP
    assert row.requires_human_confirmation is True


@pytest.mark.unit
def test_unknown_state_author_also_requires_confirmation() -> None:
    row = _one(_bundle(current_state=_state("In Progress", set_by_automation=None)))
    assert row.requires_human_confirmation is True


@pytest.mark.unit
def test_automation_set_state_flips_without_confirmation() -> None:
    row = _one(_bundle(current_state=_state("In Progress", set_by_automation=True)))
    assert row.action is EnumBoardReconcileAction.FLIP
    assert row.requires_human_confirmation is False


@pytest.mark.unit
def test_no_change_row_never_requires_confirmation() -> None:
    row = _one(
        _bundle(
            current_state=_state("In Review", set_by_automation=False),
            pull_requests=(_open_pr(),),
        )
    )
    assert row.action is EnumBoardReconcileAction.NO_CHANGE
    assert row.requires_human_confirmation is False


# --- Determinism -----------------------------------------------------------


@pytest.mark.unit
def test_running_twice_over_the_same_facts_is_byte_identical() -> None:
    bundles = (
        _bundle("OMN-A", current_state=_state("In Progress")),
        _bundle(
            "OMN-B",
            current_state=_state("Backlog", "backlog"),
            pull_requests=(_open_pr(),),
        ),
        _bundle("OMN-C", current_state=_state("In Review"), facts_complete=False),
    )
    handler = HandlerBoardTruth()
    payload = ModelBoardTruthInput(
        correlation_id=_CORRELATION,
        evaluated_at=_NOW,
        staleness_days=_STALENESS_DAYS,
        tickets=bundles,
    )
    first = handler.handle(payload)
    second = handler.handle(payload)
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.unit
def test_row_order_follows_input_order() -> None:
    rows = _run(_bundle("OMN-C"), _bundle("OMN-A"), _bundle("OMN-B"))
    assert [r.ticket for r in rows] == ["OMN-C", "OMN-A", "OMN-B"]


@pytest.mark.unit
def test_every_ticket_yields_exactly_one_row() -> None:
    rows = _run(_bundle("OMN-A"), _bundle("OMN-B"), _bundle("OMN-C"))
    assert len(rows) == 3
