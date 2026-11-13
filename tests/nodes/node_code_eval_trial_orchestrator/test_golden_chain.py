# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23627 AC11 -- golden chain: two tasks, two models, bound 3, a stub host and a
scorer passing only controls give 12 try rows, 2 control rows, 14 score requests and
one close, and the same on redelivery."""

from __future__ import annotations

import pytest

from tests.nodes.node_code_eval_trial_orchestrator.support import (
    Run,
    pass_controls_only,
    start,
)

pytestmark = pytest.mark.unit


def run_golden(*, redeliver: bool) -> Run:
    run, first = start(2, 2)
    run.drive(first, pass_controls_only, redeliver=redeliver)
    return run


def assert_golden(run: Run) -> None:
    rows = run.verdict_rows
    assert len([r for r in rows if r.is_control]) == 2
    assert len([r for r in rows if not r.is_control]) == 12
    assert len(run.score_requests) == 14
    assert len(run.closes) == 1
    assert run.row is not None
    assert run.row.closed
    assert run.row.state == "COMPLETED"
    assert not run.row.in_flight
    assert len({(r.task_key, r.model_arm, r.try_ordinal) for r in rows}) == 14


def test_golden_chain_two_tasks_two_models_bound_three() -> None:
    assert_golden(run_golden(redeliver=False))


def test_golden_chain_is_the_same_on_redelivery() -> None:
    plain, redelivered = run_golden(redeliver=False), run_golden(redeliver=True)
    assert_golden(redelivered)
    assert redelivered.closes[0].sequences == plain.closes[0].sequences
    assert [len(o.verdict_rows) for o in redelivered.outputs if o.dropped] == [0] * len(
        [o for o in redelivered.outputs if o.dropped]
    )


def test_closed_event_names_every_sequence_with_its_three_verdicts() -> None:
    closed = run_golden(redeliver=False).closes[0]
    assert len(closed.sequences) == 4
    assert {s.verdicts for s in closed.sequences} == {("fail_check",) * 3}
    assert closed.attempt_budgets == (1, 3)
    assert closed.abandoned is False
    assert closed.digest == "digest-1"
