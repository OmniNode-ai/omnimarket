# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13578: aislop_sweep must be a failing-rollup sub-job in omnimarket CI.

Per Operating Rule 5 (enforcement, not detection) and the Model B failing-rollup
enforcement established in OMN-13574, the aislop detection sweep — previously
ABSENT from omnimarket CI — must run as a required sub-job whose failure turns
the ``CI Summary`` rollup red.

These tests assert the static wiring that makes the gate real:

1. an ``aislop-sweep`` job exists in ``ci.yml``;
2. it runs the strict AI-slop scanner over the PR diff (new violations block,
   pre-existing tree debt does not — the same diff-scoped model omnibase_core
   and omniclaude use for their merge-blocking aislop gate);
3. it is a member of the ``ci-summary`` rollup ``needs`` set; and
4. the ``ci-summary`` failure loop checks the ``aislop-sweep`` result, so a
   failed scan flips the required rollup to FAILED rather than being silently
   tolerated.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.mark.unit
class TestAislopSweepGateWiring:
    """Static proof that aislop_sweep is a failing-rollup sub-job (OMN-13578)."""

    def _parsed(self) -> dict:
        return yaml.safe_load(CI_WORKFLOW.read_text())

    def test_ci_workflow_exists(self) -> None:
        assert CI_WORKFLOW.exists(), f"CI workflow not found: {CI_WORKFLOW}"

    def test_aislop_sweep_job_present(self) -> None:
        jobs = self._parsed().get("jobs", {})
        assert "aislop-sweep" in jobs, (
            f"aislop-sweep job missing from ci.yml. Jobs: {sorted(jobs)}"
        )

    def test_aislop_sweep_runs_strict_diff_scan(self) -> None:
        """The job must run the strict AI-slop scanner scoped to the PR diff."""
        content = CI_WORKFLOW.read_text()
        assert "check_ai_slop.py" in content, (
            "aislop-sweep must invoke check_ai_slop.py (canonical scanner)"
        )
        assert "--strict" in content, (
            "aislop-sweep must run the scanner in --strict mode"
        )

    def test_aislop_sweep_in_ci_summary_needs(self) -> None:
        jobs = self._parsed().get("jobs", {})
        summary = jobs.get("ci-summary", {})
        needs = summary.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "aislop-sweep" in needs, (
            f"aislop-sweep must be in ci-summary needs. needs={needs}"
        )

    def test_ci_summary_failure_loop_checks_aislop_sweep(self) -> None:
        """The rollup's result loop must include aislop-sweep so it can fail it."""
        content = CI_WORKFLOW.read_text()
        assert "aislop-sweep=${{ needs.aislop-sweep.result }}" in content, (
            "ci-summary failure loop must check needs.aislop-sweep.result so a "
            "failed scan turns the required rollup red"
        )
