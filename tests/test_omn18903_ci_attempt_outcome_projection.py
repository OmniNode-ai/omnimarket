# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18903: the pure fold over classified check outcomes.

These drive the fold directly. It takes its event time as a request field and
reads no clock, touches no broker and opens no database, so every assertion
here is a statement about a function rather than about a lane.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.merge_control.reason_code_classifier import (
    EnumCiAttemptCauseClass,
    EnumMergeCheckReasonCode,
)
from omnimarket.nodes.node_projection_ci_attempt_outcome.handlers import (
    HandlerProjectionCiAttemptOutcome,
)
from omnimarket.nodes.node_projection_ci_attempt_outcome.models import (
    ModelCiAttemptOutcomeProjectionRequest,
    parse_ticket_id,
)

pytestmark = pytest.mark.unit

_OBSERVED_AT = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
_SHA_1 = "1" * 40
_SHA_2 = "2" * 40
_SHA_3 = "3" * 40


def _check(
    name: str,
    sha: str,
    *,
    attempt: int = 1,
    code: str = "product_failed",
    affirmative: bool = True,
    step: str = "Run pytest",
) -> dict[str, object]:
    return {
        "name": name,
        "conclusion": "failure",
        "reason_code": code,
        "head_sha": sha,
        "run_attempt": attempt,
        "run_id": "5501",
        "failed_step_name": step,
        "cause_affirmative": affirmative,
    }


def _event(
    *,
    title: str = "feat(OMN-12345): a change",
    history: tuple[str, ...] = (_SHA_1,),
    checks: tuple[dict[str, object], ...] = (),
    repo: str = "OmniNode-ai/omnimarket",
    pr_number: int = 2726,
) -> dict[str, object]:
    """One inventory-completed event, in the bare shape the runtime dispatches."""
    return {
        "repo": repo,
        "pr_states": [
            {
                "repo": repo,
                "pr_number": pr_number,
                "title": title,
                "head_sha_history": list(history),
                "check_runs": list(checks),
            }
        ],
        "_envelope_timestamp": _OBSERVED_AT,
    }


def _fold(event: dict[str, object]):  # type: ignore[no-untyped-def]
    request = ModelCiAttemptOutcomeProjectionRequest.model_validate(event)
    return HandlerProjectionCiAttemptOutcome().handle(request)


# --------------------------------------------------------------------------
# AC-1: attempt ordinals are commit order.
# --------------------------------------------------------------------------


def test_three_head_commits_become_three_rows_in_commit_order() -> None:
    result = _fold(
        _event(
            history=(_SHA_1, _SHA_2, _SHA_3),
            checks=(
                _check("CI", _SHA_1),
                _check("CI", _SHA_2),
                _check("CI", _SHA_3),
            ),
        )
    )
    assert [row.attempt_ordinal for row in result.rows] == [1, 2, 3]
    assert [row.head_sha for row in result.rows] == [_SHA_1, _SHA_2, _SHA_3]


def test_ordinals_are_commit_order_not_arrival_order() -> None:
    """Rows arriving reversed still carry their commit-order ordinals.

    A window function over arrival time would give 3, 2, 1 here. The producer
    knows the commit order and the reader does not, which is why the ordinal
    is carried rather than computed at read time.
    """
    result = _fold(
        _event(
            history=(_SHA_1, _SHA_2, _SHA_3),
            checks=(
                _check("CI", _SHA_3),
                _check("CI", _SHA_2),
                _check("CI", _SHA_1),
            ),
        )
    )
    by_sha = {row.head_sha: row.attempt_ordinal for row in result.rows}
    assert by_sha == {_SHA_1: 1, _SHA_2: 2, _SHA_3: 3}


def test_a_commit_absent_from_the_declared_history_is_skipped_and_counted() -> None:
    """Guessing an ordinal would put an attempt in the wrong order silently."""
    result = _fold(_event(history=(_SHA_1,), checks=(_check("CI", _SHA_2),)))
    assert result.rows == ()
    assert result.skipped_check_count == 1


def test_a_reintroduced_commit_keeps_its_first_position() -> None:
    """A force push that reinstates an earlier commit does not renumber."""
    result = _fold(
        _event(
            history=(_SHA_1, _SHA_2, _SHA_1),
            checks=(_check("CI", _SHA_1),),
        )
    )
    assert [row.attempt_ordinal for row in result.rows] == [1]


# --------------------------------------------------------------------------
# AC-2: the ticket is parsed or null, never guessed.
# --------------------------------------------------------------------------


def test_an_unparseable_ticket_is_written_null_and_counted() -> None:
    result = _fold(
        _event(title="chore: no ticket here", checks=(_check("CI", _SHA_1),))
    )
    assert len(result.rows) == 1
    assert result.rows[0].ticket_id is None
    assert result.unattributed_row_count == 1


def test_a_title_with_a_ticket_is_the_positive_control() -> None:
    """Without this, a parser that returned null for everything would pass."""
    result = _fold(
        _event(title="feat(OMN-18903): a change", checks=(_check("CI", _SHA_1),))
    )
    assert result.rows[0].ticket_id == "OMN-18903"
    assert result.unattributed_row_count == 0


def test_a_second_ticket_in_the_title_does_not_move_the_attribution() -> None:
    """A title naming another ticket in passing must not reattribute the work."""
    assert parse_ticket_id("fix(OMN-18903): restores the OMN-14953 gate") == (
        "OMN-18903"
    )


# --------------------------------------------------------------------------
# AC-4: the carried verdict is stored, never re-derived.
# --------------------------------------------------------------------------


def test_the_carried_verdict_is_stored_rather_than_re_derived() -> None:
    """The re-run rescue survives only because the cause is carried.

    This check's step name says the test suite failed, and the classifier
    already decided it was infrastructure because a later attempt of the same
    commit reached green. A projection that re-derived the cause from the
    step name would overwrite that verdict with the wrong one, and the fact
    it needs -- that a second attempt exists -- is not in this row.
    """
    result = _fold(
        _event(
            checks=(
                _check(
                    "CI",
                    _SHA_1,
                    code="runner_infra",
                    step="Run pytest (full suite)",
                ),
            )
        )
    )
    assert result.rows[0].cause_code is EnumMergeCheckReasonCode.RUNNER_INFRA
    assert result.rows[0].failed_step_name == "Run pytest (full suite)"


def test_an_absent_provenance_flag_records_non_affirmative() -> None:
    """A producer that did not say the verdict was affirmative has not said it."""
    check = _check("CI", _SHA_1)
    del check["cause_affirmative"]
    result = _fold(_event(checks=(check,)))
    assert result.rows[0].cause_affirmative is False
    assert result.rows[0].cause_class is EnumCiAttemptCauseClass.UNKNOWN


def test_the_cause_class_is_derived_from_the_two_stored_columns() -> None:
    """A stored class could disagree with its own code; a derived one cannot."""
    result = _fold(_event(checks=(_check("CI", _SHA_1, code="process_gate_refused"),)))
    assert result.rows[0].cause_class is EnumCiAttemptCauseClass.PROCESS


# --------------------------------------------------------------------------
# Consumer-first, and the skip paths.
# --------------------------------------------------------------------------


def test_a_check_with_no_attempt_identity_yields_no_row_and_is_counted() -> None:
    """The consumer-first state: deployed before the producer emits.

    Until the producer ships, every check arrives without an attempt
    identity and this projection writes nothing. That is the intended state
    and it is COUNTED, so "wrote nothing" and "was given nothing" stay
    distinguishable.
    """
    result = _fold(
        _event(
            checks=(
                {
                    "name": "CI",
                    "conclusion": "failure",
                    "reason_code": "product_failed",
                },
            )
        )
    )
    assert result.rows == ()
    assert result.skipped_check_count == 1


def test_a_green_check_yields_no_row() -> None:
    result = _fold(_event(checks=({"name": "CI", "conclusion": "success"},)))
    assert result.rows == ()


def test_an_event_from_an_older_producer_validates() -> None:
    """A payload with no new field at all must not be refused."""
    request = ModelCiAttemptOutcomeProjectionRequest.model_validate(
        {
            "repo": "OmniNode-ai/omnimarket",
            "pr_states": [
                {
                    "repo": "OmniNode-ai/omnimarket",
                    "pr_number": 1,
                    "title": "feat(OMN-1): x",
                    "check_runs": [{"name": "CI", "status": "completed"}],
                }
            ],
            "_envelope_timestamp": _OBSERVED_AT,
        }
    )
    assert HandlerProjectionCiAttemptOutcome().handle(request).rows == ()


# --------------------------------------------------------------------------
# Purity.
# --------------------------------------------------------------------------


def test_replaying_the_same_event_derives_identical_rows() -> None:
    event = _event(
        history=(_SHA_1, _SHA_2),
        checks=(_check("CI", _SHA_1), _check("Lint", _SHA_2)),
    )
    assert _fold(event) == _fold(event)


def test_the_fold_takes_its_event_time_as_input_and_reads_no_clock() -> None:
    """Same input, same observed_at, whenever it runs."""
    result = _fold(_event(checks=(_check("CI", _SHA_1),)))
    assert result.rows[0].observed_at == _OBSERVED_AT
