# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_receipt_gate_pin_friction: the pure fold [OMN-20134].

One planted case per outcome the ruling of 2026-10-31T00:13:02Z asked to measure:
a pull request opened and merged untouched, merged after a fix push, merged after
a CI re-run, an updater run that failed or was refused, an audit, a guard and a
conflict refusal, a redelivery, and a closed pull request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.receipt_gate_pin_friction import (
    EnumPinFrictionReason,
    EnumPinKind,
    EnumPinOutcomeKind,
    EnumPinPrState,
    EnumPinRefusalClass,
    ModelPinCheckObservation,
    ModelPinFrictionRow,
    ModelPinHeadObservation,
    ModelPinMergedObservation,
    ModelPinOutcomeEvent,
    ModelPinRunObservation,
)
from omnimarket.nodes.node_projection_receipt_gate_pin_friction.handlers.handler_pin_friction_reducer import (
    load_friction_config,
)
from omnimarket.nodes.node_projection_receipt_gate_pin_friction.reducers import (
    FoldResult,
    PinObservation,
    fold_pin_observation,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_receipt_gate_pin_friction"
    / "contract.yaml"
)
CONFIG = load_friction_config(yaml.safe_load(CONTRACT.read_text()))

REPO = "OmniNode-ai/omnimarket"
TAG = "dev-20261030152335-66d35d2283c3"
BRANCH = f"pin-advance/receipt-gate/{TAG}"
PUSHED = datetime(2026, 10, 31, 1, 0, 0, tzinfo=UTC)
OPENED = PUSHED + timedelta(seconds=90)
HEAD0 = "a" * 40
HEAD1 = "b" * 40


def _outcome(**fields: object) -> ModelPinOutcomeEvent:
    base: dict[str, object] = {
        "outcome": EnumPinOutcomeKind.OPENED,
        "pin_kind": EnumPinKind.TAG,
        "repository": REPO,
        "core_tag": TAG,
        "branch": BRANCH,
        "branch_head_sha": HEAD0,
        "pr_number": 7,
        "tag_pushed_at": PUSHED,
        "outcome_at": OPENED,
        "seconds_tag_to_pr_opened": 90.0,
    }
    return ModelPinOutcomeEvent.model_validate({**base, **fields})


def _run(row: ModelPinFrictionRow | None, *obs: PinObservation) -> FoldResult:
    result = FoldResult(row=row)
    friction = []
    for observation in obs:
        result = fold_pin_observation(result.row, observation, CONFIG)
        friction.extend(result.friction)
    return FoldResult(row=result.row, friction=tuple(friction))


def _opened(**fields: object) -> ModelPinFrictionRow:
    row = _run(None, _outcome(**fields)).row
    assert row is not None
    return row


def _merged(at: datetime) -> ModelPinMergedObservation:
    return ModelPinMergedObservation(
        repo=REPO, branch=BRANCH, pr_number=7, merged_at=at
    )


def _head(sha: str, at: datetime, **fields: object) -> ModelPinHeadObservation:
    return ModelPinHeadObservation.model_validate(
        {
            "repo": REPO,
            "head_ref": BRANCH,
            "pr_number": 7,
            "as_of": at,
            "head_sha": sha,
            **fields,
        }
    )


def _attempt(
    attempt: int | None, at: datetime, *, run_id: int = 99, sha: str = HEAD0
) -> ModelPinRunObservation:
    return ModelPinRunObservation(
        repo=REPO,
        run_id=run_id,
        head_sha=sha,
        status="requested",
        as_of=at,
        run_attempt=attempt,
    )


def test_an_opened_pull_request_becomes_a_row_with_its_pin_kind_and_tag_to_pr_time() -> (
    None
):
    result = _run(None, _outcome(pin_kind=EnumPinKind.SHA))
    row = result.row
    assert row is not None
    assert (row.repository, row.branch, row.core_tag) == (REPO, BRANCH, TAG)
    assert row.pin_kind is EnumPinKind.SHA
    assert row.state is EnumPinPrState.OPENED
    assert row.pr_number == 7
    assert row.tag_pushed_at == PUSHED
    assert row.pr_opened_at == OPENED
    assert row.seconds_tag_to_pr_opened == 90.0
    assert row.head_shas == (HEAD0,)
    assert (row.fix_pushes, row.ci_reruns, row.touches) == (0, 0, 0)
    assert result.friction == ()


def test_a_pull_request_that_merges_untouched_closes_the_row_with_its_times() -> None:
    merged_at = OPENED + timedelta(minutes=12)
    result = _run(None, _outcome(), _head(HEAD0, OPENED), _merged(merged_at))
    row = result.row
    assert row is not None
    assert row.state is EnumPinPrState.MERGED
    assert row.merged_untouched
    assert row.seconds_pr_open_to_merge == 720.0
    assert row.seconds_tag_to_merge == 810.0
    assert result.friction == ()


def test_a_fix_push_is_a_touch_and_flags_friction_once() -> None:
    result = _run(
        None,
        _outcome(),
        _head(HEAD1, OPENED + timedelta(minutes=3)),
        _head(HEAD1, OPENED + timedelta(minutes=4)),
        _merged(OPENED + timedelta(minutes=10)),
    )
    row = result.row
    assert row is not None
    assert row.head_shas == (HEAD0, HEAD1)
    assert (row.fix_pushes, row.ci_reruns, row.touches) == (1, 0, 1)
    assert row.state is EnumPinPrState.MERGED
    assert not row.merged_untouched
    assert [e.reason for e in result.friction] == [EnumPinFrictionReason.HUMAN_TOUCHED]
    flagged = result.friction[0]
    assert (flagged.site, flagged.pin_kind, flagged.core_tag) == (
        REPO,
        EnumPinKind.TAG,
        TAG,
    )
    assert (flagged.fix_pushes, flagged.ci_reruns, flagged.pr_number) == (1, 0, 7)
    assert row.friction_reasons == (EnumPinFrictionReason.HUMAN_TOUCHED,)


def test_a_ci_rerun_of_a_head_of_the_pull_request_is_a_touch() -> None:
    at = OPENED + timedelta(minutes=2)
    result = _run(None, _outcome(), _attempt(2, at), _attempt(2, at))
    row = result.row
    assert row is not None
    assert row.rerun_keys == ("99:2",)
    assert (row.fix_pushes, row.ci_reruns, row.touches) == (0, 1, 1)
    assert [e.reason for e in result.friction] == [EnumPinFrictionReason.HUMAN_TOUCHED]
    assert result.friction[0].ci_reruns == 1


def test_a_first_attempt_an_unknown_head_and_a_missing_attempt_are_not_reruns() -> None:
    at = OPENED + timedelta(minutes=2)
    result = _run(
        None,
        _outcome(),
        _attempt(1, at),
        _attempt(None, at),
        _attempt(2, at, sha="c" * 40),
    )
    assert result.row is not None
    assert result.row.touches == 0
    assert result.friction == ()


def test_a_failed_updater_run_is_a_failed_row_with_its_refusal_and_flags_friction() -> (
    None
):
    result = _run(
        None,
        _outcome(
            outcome=EnumPinOutcomeKind.FAILED,
            branch_head_sha=None,
            pr_number=None,
            seconds_tag_to_pr_opened=None,
            refusal_class=EnumPinRefusalClass.GUARD,
            detail="pr_create for x was refused: quota_floor",
        ),
    )
    row = result.row
    assert row is not None
    assert row.state is EnumPinPrState.FAILED
    assert row.pr_opened_at is None
    assert [r.refusal_class for r in row.refusals] == [EnumPinRefusalClass.GUARD]
    assert [e.reason for e in result.friction] == [EnumPinFrictionReason.UPDATER_FAILED]
    assert result.friction[0].refusal_classes == (EnumPinRefusalClass.GUARD,)
    assert "quota_floor" in result.friction[0].detail


def test_a_refused_updater_run_is_a_refused_row_and_flags_friction() -> None:
    result = _run(
        None,
        _outcome(
            outcome=EnumPinOutcomeKind.REFUSED,
            branch_head_sha=None,
            pr_number=None,
            seconds_tag_to_pr_opened=None,
            refusal_class=EnumPinRefusalClass.UNMATCHED,
            detail="pin_site_unmatched: no pin line",
        ),
    )
    assert result.row is not None
    assert result.row.state is EnumPinPrState.REFUSED
    assert [e.reason for e in result.friction] == [EnumPinFrictionReason.UPDATER_FAILED]


def test_a_retry_that_opens_the_pull_request_keeps_the_history_and_flags_nothing_new() -> (
    None
):
    failed = _outcome(
        outcome=EnumPinOutcomeKind.FAILED,
        branch_head_sha=None,
        pr_number=None,
        seconds_tag_to_pr_opened=None,
        refusal_class=EnumPinRefusalClass.OTHER,
        detail="socket reset",
    )
    retry_at = OPENED + timedelta(minutes=30)
    result = _run(
        None,
        failed,
        _outcome(outcome_at=retry_at, seconds_tag_to_pr_opened=1890.0),
    )
    row = result.row
    assert row is not None
    assert row.state is EnumPinPrState.OPENED
    assert row.pr_opened_at == retry_at
    assert row.seconds_tag_to_pr_opened == 1890.0
    assert row.friction_reasons == (EnumPinFrictionReason.UPDATER_FAILED,)
    assert len(result.friction) == 1
    assert len(row.refusals) == 1


def test_a_red_audit_check_and_a_red_guard_check_are_recorded_as_refusals() -> None:
    at = OPENED + timedelta(minutes=1)

    def red(check: str, conclusion: str = "failure") -> ModelPinCheckObservation:
        return ModelPinCheckObservation(
            repo=REPO,
            pr_number=7,
            head_sha=HEAD0,
            check=check,
            conclusion=conclusion,
            completed_at=at,
        )

    result = _run(
        None,
        _outcome(),
        red("audit-runner-routing / audit"),
        red("sibling-ref guard"),
        red("lint"),
        red("sibling-ref guard", "success"),
    )
    row = result.row
    assert row is not None
    assert [r.refusal_class for r in row.refusals] == [
        EnumPinRefusalClass.AUDIT,
        EnumPinRefusalClass.GUARD,
    ]
    assert row.touches == 0
    assert result.friction == ()


def test_a_conflicting_pull_request_is_recorded_as_a_conflict_refusal_once() -> None:
    at = OPENED + timedelta(minutes=5)
    result = _run(
        None,
        _outcome(),
        _head(HEAD0, at, mergeable="CONFLICTING"),
        _head(HEAD0, at + timedelta(minutes=1), mergeable="CONFLICTING"),
    )
    assert result.row is not None
    assert [r.refusal_class for r in result.row.refusals] == [
        EnumPinRefusalClass.CONFLICT
    ]


def test_a_closed_unmerged_pull_request_is_closed_and_later_pushes_do_not_count() -> (
    None
):
    at = OPENED + timedelta(minutes=5)
    result = _run(
        None,
        _outcome(),
        _head(HEAD0, at, triage_state="closed"),
        _head(HEAD1, at + timedelta(minutes=1)),
    )
    assert result.row is not None
    assert result.row.state is EnumPinPrState.CLOSED
    assert result.row.closed_at == at
    assert result.row.touches == 0


def test_a_redelivered_tag_and_a_duplicate_merge_change_nothing() -> None:
    merged_at = OPENED + timedelta(minutes=12)
    once = _run(None, _outcome(), _merged(merged_at))
    again = _run(
        once.row,
        _outcome(outcome=EnumPinOutcomeKind.REPLAYED),
        _outcome(),
        _merged(merged_at + timedelta(minutes=1)),
    )
    assert again.row == once.row
    assert again.friction == ()


def test_an_observation_for_a_branch_the_updater_never_reported_is_nothing() -> None:
    result = fold_pin_observation(None, _merged(OPENED), CONFIG)
    assert result.row is None
    assert result.friction == ()


def test_the_fold_is_deterministic_over_a_replay() -> None:
    sequence: tuple[PinObservation, ...] = (
        _outcome(),
        _head(HEAD1, OPENED + timedelta(minutes=3)),
        _attempt(2, OPENED + timedelta(minutes=4), sha=HEAD1),
        _merged(OPENED + timedelta(minutes=9)),
    )
    assert _run(None, *sequence) == _run(None, *sequence)
    row = _run(None, *sequence).row
    assert row is not None
    assert row.touches == 2


def test_the_contract_config_classes_red_checks_by_name() -> None:
    assert {c.refusal_class.value for c in CONFIG.check_refusal_classes} == {
        "audit",
        "guard",
    }
    assert "failure" in CONFIG.red_conclusions
    assert CONFIG.conflicting_mergeable == "CONFLICTING"
