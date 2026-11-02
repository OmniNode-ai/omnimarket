# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22176: the Hostile Review job accepts an agent receipt only through ``pass_agent``.

One test per decision of the gate-accept node. Each runs the workflow's own review,
accept and enforcement steps and its gate job (see ``hostile_review_job``), with the
node faked to answer the decision under test, across the three voter verdicts, and asserts
the conclusion of every required context of the repository:

* a ``degraded`` voter result is green only with ``pass_agent``;
* a ``passed`` voter result is green whatever the node answers short of a failure;
* a ``blocked`` voter result is red whatever the node answers, ``pass_agent`` included.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.ci.hostile_review_job import (
    Run,
    required_context_names,
    simulate,
)

pytestmark = pytest.mark.unit


def _run(tmp_path: Path, name: str, verdict: str, decision: str) -> Run:
    work = tmp_path / f"{name}-{verdict}-{decision}"
    work.mkdir()
    return simulate(work, verdict=verdict, decision=decision)


def _assert_contexts(run: Run, *, green: bool, case: str) -> None:
    required = required_context_names()
    assert required, "the repository lists no required Hostile Reviewer context"
    assert set(required) == set(run.contexts), (
        "the simulated contexts must be exactly the required ones: "
        f"{sorted(required)} vs {sorted(run.contexts)}"
    )
    for context in required:
        assert run.green(context) is green, (
            f"{case}: required context {context!r} should be "
            f"{'green' if green else 'red'}; steps={run.exits} skipped={run.skipped}"
        )


def test_unchanged_leaves_the_voters_verdict_to_decide(tmp_path: Path) -> None:
    passed = _run(tmp_path, "u", "passed", "unchanged")
    _assert_contexts(passed, green=True, case="passed voters, unchanged")
    degraded = _run(tmp_path, "u", "degraded", "unchanged")
    _assert_contexts(degraded, green=False, case="degraded voters, unchanged")
    assert "enforce" in degraded.exits, "the enforcement step runs on unchanged"
    blocked = _run(tmp_path, "u", "blocked", "unchanged")
    _assert_contexts(blocked, green=False, case="blocked voters, unchanged")


def test_pass_agent_replaces_degraded_voters_and_nothing_else(tmp_path: Path) -> None:
    degraded = _run(tmp_path, "p", "degraded", "pass_agent")
    _assert_contexts(degraded, green=True, case="degraded voters, pass_agent")
    assert "enforce" in degraded.skipped, "pass_agent skips the enforcement step"
    passed = _run(tmp_path, "p", "passed", "pass_agent")
    _assert_contexts(passed, green=True, case="passed voters, pass_agent")
    blocked = _run(tmp_path, "p", "blocked", "pass_agent")
    _assert_contexts(blocked, green=False, case="blocked voters, pass_agent")


def test_refuse_leaves_a_degraded_or_blocked_verdict_red(tmp_path: Path) -> None:
    degraded = _run(tmp_path, "r", "degraded", "refuse")
    _assert_contexts(degraded, green=False, case="degraded voters, refuse")
    assert "enforce" in degraded.exits, "the enforcement step runs on refuse"
    blocked = _run(tmp_path, "r", "blocked", "refuse")
    _assert_contexts(blocked, green=False, case="blocked voters, refuse")
    passed = _run(tmp_path, "r", "passed", "refuse")
    _assert_contexts(passed, green=True, case="passed voters, refuse")
