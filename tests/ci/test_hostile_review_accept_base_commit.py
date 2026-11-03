# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22176: the gate-accept step runs the node from the base commit, and fails closed.

A pull request cannot supply the code that accepts itself: the step runs the node from a
worktree of the base commit, and when that base does not carry the node (the case of the
pull request that adds it), cannot be checked out, or the node does not run, the step
reports ``decision=unchanged`` and the voters' verdict stands. The accept step itself
never fails the job.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.ci.hostile_review_job import Run, required_context_names, simulate

pytestmark = pytest.mark.unit


def _run(
    tmp_path: Path,
    name: str,
    *,
    verdict: str,
    decision: str,
    base_has_node: bool = True,
    base_checkout_fails: bool = False,
    node_exit: int = 0,
) -> Run:
    work = tmp_path / name
    work.mkdir()
    return simulate(
        work,
        verdict=verdict,
        decision=decision,
        base_has_node=base_has_node,
        base_checkout_fails=base_checkout_fails,
        node_exit=node_exit,
    )


def _all(run: Run, *, green: bool) -> None:
    for context in required_context_names():
        assert run.green(context) is green, (context, run.exits, run.skipped)


def test_the_node_is_run_from_the_base_worktree_with_this_attempts_files(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path, "base", verdict="degraded", decision="pass_agent")
    assert len(run.node_calls) == 1
    assert run.outputs["accept"]["decision"] == "pass_agent"
    assert run.exits["accept"] == 0
    _all(run, green=True)


@pytest.mark.parametrize("verdict", ["passed", "degraded", "blocked"])
def test_a_base_without_the_node_leaves_the_voters_verdict_to_decide(
    tmp_path: Path, verdict: str
) -> None:
    run = _run(
        tmp_path,
        verdict,
        verdict=verdict,
        decision="pass_agent",
        base_has_node=False,
    )
    assert run.node_calls == [], "the pull request's own node must never run"
    assert run.exits["accept"] == 0
    assert run.outputs["accept"] == {"decision": "unchanged"}
    _all(run, green=verdict == "passed")


def test_a_base_that_cannot_be_checked_out_leaves_the_voters_verdict_to_decide(
    tmp_path: Path,
) -> None:
    run = _run(
        tmp_path,
        "checkout",
        verdict="degraded",
        decision="pass_agent",
        base_checkout_fails=True,
    )
    assert run.node_calls == []
    assert run.exits["accept"] == 0
    assert run.outputs["accept"] == {"decision": "unchanged"}
    _all(run, green=False)


@pytest.mark.parametrize("verdict", ["passed", "degraded"])
def test_a_node_that_does_not_run_never_fails_the_job_by_itself(
    tmp_path: Path, verdict: str
) -> None:
    run = _run(
        tmp_path,
        verdict,
        verdict=verdict,
        decision="pass_agent",
        node_exit=2,
    )
    assert len(run.node_calls) == 1
    assert run.exits["accept"] == 0
    assert run.outputs["accept"] == {"decision": "unchanged"}
    _all(run, green=verdict == "passed")
