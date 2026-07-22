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
   and omniclaude use for their merge-blocking aislop gate); and
3. its result is enforced by ``CI Summary``.

OMN-14127 fan-out (CI-G2): ``ci-summary`` migrated from a ``needs``-gated shell
loop to a NO-``needs`` fail-closed poller (``scripts/ci/ci_summary_gate.py``).
Enforcement of ``aislop-sweep`` therefore moved OUT of the YAML ``needs`` loop
and INTO the poller's ``STRICT_GATE_JOBS`` anchor (by the job's DISPLAY name).
These tests assert that relocation preserved enforcement rather than dropping
it: ``aislop-sweep`` is still a strict, must-be-``success`` gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.ci.ci_summary_gate import STRICT_GATE_JOBS

REPO_ROOT = Path(__file__).parent.parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The Actions jobs API DISPLAY name for the ``aislop-sweep`` job key.
AISLOP_SWEEP_DISPLAY_NAME = "Aislop Sweep (strict, PR diff)"


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

    def test_ci_summary_is_no_needs_poller(self) -> None:
        """OMN-14127: ci-summary must be a NO-``needs`` poller (never wedges)."""
        summary = self._parsed().get("jobs", {}).get("ci-summary", {})
        assert "needs" not in summary, (
            "ci-summary must have NO `needs:` — a needs-gated required context "
            "goes absent under fleet saturation and wedges the PR forever "
            "(OMN-14127). Enforcement lives in scripts/ci/ci_summary_gate.py."
        )
        content = CI_WORKFLOW.read_text()
        assert "scripts/ci/ci_summary_gate.py" in content, (
            "ci-summary must invoke the fail-closed poller gate script"
        )

    def test_aislop_sweep_is_strict_gate_in_poller(self) -> None:
        """aislop-sweep enforcement moved into the poller's STRICT anchor.

        A strict gate must be present + completed + EXACTLY ``success``; a
        skipped/cancelled/failed aislop scan fails the required CI Summary
        context — the same enforcement the old ``needs`` loop provided, now
        relocated to the poller (by display name).
        """
        assert AISLOP_SWEEP_DISPLAY_NAME in STRICT_GATE_JOBS, (
            f"{AISLOP_SWEEP_DISPLAY_NAME!r} must be in STRICT_GATE_JOBS so a "
            "failed/skipped aislop scan turns the required CI Summary red. "
            f"STRICT_GATE_JOBS={STRICT_GATE_JOBS}"
        )
