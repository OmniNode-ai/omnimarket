# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation nightly's job budget must outlast its own corpus (OMN-18349).

A job cancelled on ``timeout-minutes`` never reaches its ``if: always()``
artifact uploads, so a red night leaves no scoreboard and no JUnit report --
the AC3 failure this ticket already fixed once, arriving the second time from
the job budget rather than from the runner.

This module is the CI half of ``scripts/ci/check_nightly_corpus_budget.py``: the
same ``check_budget`` predicate the pre-commit hook runs, reached from inside the
required pytest job so a local pass and a CI pass cannot disagree. Every
assertion carries a falsification control, because a checker that cannot be
shown to fail is not evidence that the thing it checks is true.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.ci.check_nightly_corpus_budget import (
    NIGHTLY_JOB_ID,
    NIGHTLY_WORKFLOW,
    SETUP_ALLOWANCE_MINUTES,
    check_budget,
    declared_timeout_minutes,
    required_timeout_minutes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _workflow_copy(tmp_path: Path, mutate: object) -> Path:
    """A throwaway repo root holding a mutated copy of the nightly workflow."""
    source = REPO_ROOT / NIGHTLY_WORKFLOW
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    mutate(document)  # type: ignore[operator]
    target = tmp_path / NIGHTLY_WORKFLOW
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    return tmp_path


@pytest.mark.unit
class TestNightlyJobBudget:
    """The declared budget clears the derived ceiling, and the check can fail."""

    def test_repo_has_no_budget_violations(self) -> None:
        assert check_budget() == []

    def test_required_minutes_are_derived_not_written_down(self) -> None:
        """The number comes from the corpus, the contract and the overlay."""
        import math

        from tests.delegation_golden.corpus_loader import load_corpus
        from tests.delegation_golden.runner import corpus_wall_clock_ceiling_s

        case_count = len(list(load_corpus().integration_cases()))
        expected = (
            math.ceil(corpus_wall_clock_ceiling_s(case_count) / 60.0)
            + SETUP_ALLOWANCE_MINUTES
        )
        assert required_timeout_minutes() == expected

    def test_declared_budget_clears_the_ceiling(self) -> None:
        declared = declared_timeout_minutes()
        assert declared is not None
        assert declared >= required_timeout_minutes()

    def test_checker_rejects_the_budget_this_job_used_to_carry(
        self, tmp_path: Path
    ) -> None:
        """Falsification control: the literal 45 the workflow carried until now.

        45 minutes was sized to a probe that published all nine cases at once
        and waited one deadline for all of them. Paced to the lane, the same
        corpus can legitimately run well past that.
        """

        def mutate(document: dict) -> None:
            document["jobs"][NIGHTLY_JOB_ID]["timeout-minutes"] = 45

        problems = check_budget(_workflow_copy(tmp_path, mutate))
        assert problems, "a 45-minute budget must be refused"
        assert "timeout-minutes: 45" in problems[0]

    def test_checker_rejects_a_job_with_no_declared_budget(
        self, tmp_path: Path
    ) -> None:
        """GitHub's 360-minute default is not a reviewed number."""

        def mutate(document: dict) -> None:
            document["jobs"][NIGHTLY_JOB_ID].pop("timeout-minutes", None)

        problems = check_budget(_workflow_copy(tmp_path, mutate))
        assert problems, "an absent timeout-minutes must be refused"
        assert "declares no integer timeout-minutes" in problems[0]

    def test_checker_accepts_a_budget_above_the_ceiling(self, tmp_path: Path) -> None:
        """Positive control: the checker is not simply always failing."""

        def mutate(document: dict) -> None:
            document["jobs"][NIGHTLY_JOB_ID]["timeout-minutes"] = (
                required_timeout_minutes() + 1
            )

        assert check_budget(_workflow_copy(tmp_path, mutate)) == []
