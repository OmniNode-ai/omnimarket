# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16235: the arch-lint PR-comment step must never override a clean verdict.

``pr-arch-review.yml``'s ``arch-lint`` job's "Post architectural lint summary as
PR comment" step posts via ``github.token``. GitHub force-downgrades
``GITHUB_TOKEN`` to read-only for ``pull_request`` runs triggered from a fork
(the workflow's own ``permissions: pull-requests: write`` cannot override that
platform restriction, and ``ONEXBOT_APP_ID``/``ONEXBOT_APP_PRIVATE_KEY`` are
themselves secrets withheld from fork-triggered runs too), so the comment call
403s ("Resource not accessible by integration") on every external/fork PR
regardless of the lint result. Without ``continue-on-error``, that 403 failed
the whole ``arch-lint`` job even when the ``lint`` step found zero violations,
which then failed ``PR Arch Review Gate`` (reads ``needs.arch-lint.result``) on
content the lint itself never flagged -- observed repeatedly on
omnimarket#2088 (a fork PR), 2026-08-18/19/20.

This pins the fix structurally against the committed YAML: the reporting step
is best-effort (``continue-on-error: true``), while the ``lint`` step that
actually sets ``ARCH_LINT_ERRORS``/exits 1 on a real violation is NOT, so the
job's pass/fail still binds to the lint result alone -- a synthetic violation
must still fail the job RED (negative control via the exit-1 gate remaining
in place, not exercised by this static test but proven by the CI job's own
`if [ "$ERRORS" -gt 0 ]; then exit 1; fi`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_WORKFLOW = (
    Path(__file__).resolve().parents[3] / ".github" / "workflows" / "pr-arch-review.yml"
)


def _load() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8")))


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    for step in job["steps"]:
        if step.get("name") == name:
            return cast("dict[str, Any]", step)
    raise AssertionError(f"step {name!r} not found in job")


@pytest.mark.unit
def test_comment_step_is_continue_on_error() -> None:
    """The report-only comment POST must never fail the job on its own."""
    job = _load()["jobs"]["arch-lint"]
    step = _step(job, "Post architectural lint summary as PR comment")
    assert step.get("continue-on-error") is True, (
        "the PR-comment step must be continue-on-error: true so a "
        "GITHUB_TOKEN 403 on fork PRs cannot fail a zero-violation lint job "
        "(OMN-16235)"
    )


@pytest.mark.unit
def test_lint_step_is_not_continue_on_error() -> None:
    """The actual lint verdict must still be able to fail the job."""
    job = _load()["jobs"]["arch-lint"]
    step = _step(job, "Run architectural lint")
    assert step.get("continue-on-error") is not True, (
        "the lint step itself must remain blocking -- a real violation "
        "(exit 1 on ERRORS>0) must still fail the arch-lint job (negative "
        "control for OMN-16235)"
    )


@pytest.mark.unit
def test_gate_job_reads_arch_lint_job_result() -> None:
    """PR Arch Review Gate must key off the job result, not a step result."""
    gate_job = _load()["jobs"]["pr-arch-review-gate"]
    run_step = gate_job["steps"][0]
    assert "needs.arch-lint.result" in run_step["run"], (
        "PR Arch Review Gate must read needs.arch-lint.result -- with "
        "continue-on-error on the comment step, that job-level result now "
        "reflects the lint step alone"
    )
