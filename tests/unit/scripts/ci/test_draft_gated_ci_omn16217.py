# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16217: native draft-state CI admission gate on omnimarket.

Org-wide fan-out (OMN-16214) of the draft-state CI admission gate design
proven on onex_change_control#6686 / OMN-15731. omnimarket has NO
``ci:ready``-style merge-intent label pilot (unlike ``onex_change_control``),
so this is a PURE draft gate: the primary (and only) admission signal is
native PR draft state (``!github.event.pull_request.draft``) — there is no
dual-accept fallback arm to test for.

The gated job is ``coverage-sweep-gate`` (display name "Coverage Sweep
Gate") — this file's own ``ci.yml`` concurrency-group comment already
documents it as "the longest-pole job (3 fresh sibling clones + `uv sync
--all-extras` + full `pytest --cov`)", i.e. the single most expensive
unconditional job in the workflow, and the closest omnimarket analog to
OCC's ``pre-commit`` gate target.

Two things are proven here, mirroring the OCC pilot's test structure
(``TestLabelGateWorkflowShape`` / ``TestDraftStateGateMigrationOmn15731Revision``
in onex_change_control, adapted for a pure draft gate with no label arm):

1. The workflow YAML shape: the job's ``if:`` carries the draft-state arm,
   scoped to dev-targeting PRs only, with the main/hotfix/push/merge_group
   promotion-boundary carve-out intact and NO dual-accept label clause.
2. The fail-closed proof: ``Coverage Sweep Gate`` stays in
   ``STRICT_GATE_JOBS`` (never ``SKIPPABLE_GATE_JOBS``), so a ``skipped``
   conclusion — draft-induced or otherwise — still fails ``CI Summary``
   closed. This is exercised directly against ``evaluate()`` so the proof is
   an executable test, not a read of the source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts.ci.ci_summary_gate import (
    EXIT_FAILURE,
    EXIT_SUCCESS,
    SELF_JOB_NAME,
    SKIPPABLE_GATE_JOBS,
    STRICT_GATE_JOBS,
    evaluate,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

pytestmark = pytest.mark.unit

_JOB_ID = "coverage-sweep-gate"
_JOB_NAME = "Coverage Sweep Gate"

# RED-before control: the pre-OMN-16217 condition string. Pinned so a future
# revert is caught by drift, not by eyeballing history.
_PRE_MIGRATION_CONDITION = "always()"


def _ci_yaml() -> dict[Any, Any]:
    return cast("dict[Any, Any]", yaml.safe_load(CI_WORKFLOW.read_text()))


def _job() -> dict[str, Any]:
    return cast("dict[str, Any]", _ci_yaml()["jobs"][_JOB_ID])


class TestDraftGateWorkflowShape:
    """Pins the ``ci.yml`` YAML shape of the OMN-16217 admission gate."""

    def test_pull_request_trigger_includes_ready_for_review(self) -> None:
        """A draft->ready flip must re-evaluate the gate on the current head
        without requiring a new push (root brief invariant: undrafting a PR
        always gets a full run before merge, never a stale skip)."""
        workflow = _ci_yaml()
        pr_trigger = workflow[True]["pull_request"]
        assert "ready_for_review" in pr_trigger["types"]
        # The pre-existing default types must survive the addition.
        for event_type in ("opened", "synchronize", "reopened"):
            assert event_type in pr_trigger["types"]

    def test_coverage_sweep_gate_if_is_gated_on_draft_state_for_dev_only(
        self,
    ) -> None:
        job = _job()
        condition = str(job["if"])
        assert "always()" in condition
        assert "github.event_name != 'pull_request'" in condition
        assert "github.base_ref != 'dev'" in condition
        assert "!github.event.pull_request.draft" in condition

    def test_no_dual_accept_label_arm(self) -> None:
        """omnimarket has no merge-intent-label pilot (unlike OCC's
        ``ci:ready``) — the gate must be a PURE draft gate with no label
        fallback clause. This is the one deliberate divergence from the OCC
        reference mechanism, called out explicitly in the ticket."""
        condition = str(_job()["if"])
        assert "labels" not in condition
        assert "ci:ready" not in condition

    def test_main_and_hotfix_boundary_carveout_survives(self) -> None:
        """The dev->main promotion-boundary guarantee (root CLAUDE.md rule
        #4) must be untouched: main/hotfix-targeting PRs, pushes, and
        merge_group runs still always run coverage-sweep-gate unconditionally,
        draft or not."""
        condition = str(_job()["if"])
        # A non-pull_request event (push/merge_group) short-circuits true.
        assert "github.event_name != 'pull_request'" in condition
        # A non-dev base_ref (main/hotfix) short-circuits true.
        assert "github.base_ref != 'dev'" in condition

    def test_condition_is_a_flat_or_chain(self) -> None:
        """The three carve-outs (non-PR event / non-dev base / non-draft)
        must be OR'd, not nested under an `&&` that would make one a
        precondition for another."""
        condition = str(_job()["if"])
        or_clause_start = condition.index("(github.event_name")
        or_clause = condition[or_clause_start:]
        assert or_clause.count("||") >= 2, (
            "expected a flat OR chain (event_name / base_ref / !draft) — "
            f"got: {or_clause}"
        )

    def test_needs_occ_preflight_unchanged(self) -> None:
        """The gate migration must not touch the job's `needs:` edge — this
        is purely a draft-state `if:` addition, not a dependency change."""
        assert _job()["needs"] == "occ-preflight"

    def test_red_control_pre_migration_shape_had_no_draft_arm(self) -> None:
        """RED-before control: the job's original condition was a bare
        ``always()`` — no draft clause, no base_ref clause at all. A draft PR
        against dev was previously ADMITTED (ran the full expensive job)
        purely because ``always()`` held. This is the state OMN-16217
        replaces; it is not a live assertion against ci.yml (which now has
        the gated condition) — it pins the pre-migration string so a future
        revert is caught by drift, not by eyeballing history."""
        assert "!github.event.pull_request.draft" not in _PRE_MIGRATION_CONDITION
        live_condition = str(_job()["if"])
        assert live_condition != _PRE_MIGRATION_CONDITION, (
            "live ci.yml condition must have moved past the pre-migration "
            "bare `always()` shape captured above"
        )


class TestDraftGateFailsClosedViaCiSummary:
    """Proves acceptance criterion (b): a draft dev PR's gate context reads
    red (or pending), never green-by-skip — exercised against the actual
    ``evaluate()`` verdict function, not merely read from source."""

    def _healthy_jobs(self) -> list[dict[str, object]]:
        jobs: list[dict[str, object]] = []
        for name in STRICT_GATE_JOBS:
            jobs.append(
                {
                    "name": name,
                    "status": "completed",
                    "conclusion": "success",
                    "run_attempt": 1,
                }
            )
        for name in SKIPPABLE_GATE_JOBS:
            jobs.append(
                {
                    "name": name,
                    "status": "completed",
                    "conclusion": "success",
                    "run_attempt": 1,
                }
            )
        jobs.append(
            {
                "name": "Tests (Split 1/1)",
                "status": "completed",
                "conclusion": "success",
                "run_attempt": 1,
            }
        )
        jobs.append(
            {
                "name": SELF_JOB_NAME,
                "status": "in_progress",
                "conclusion": None,
                "run_attempt": 1,
            }
        )
        return jobs

    def test_coverage_sweep_gate_is_strict_not_skippable(self) -> None:
        assert _JOB_NAME in STRICT_GATE_JOBS
        assert _JOB_NAME not in SKIPPABLE_GATE_JOBS

    def test_fully_healthy_snapshot_is_success(self) -> None:
        """Control: a non-draft PR where every gate (including Coverage
        Sweep Gate) ran and passed is SUCCESS."""
        code, report = evaluate(self._healthy_jobs())
        assert code == EXIT_SUCCESS, report

    def test_draft_pr_skip_of_coverage_sweep_gate_fails_closed(self) -> None:
        """The scenario this ticket exists to prove: a draft dev PR skips
        `coverage-sweep-gate` (via the new `if:`). Because the job stays in
        STRICT_GATE_JOBS, `skipped` is not an acceptable conclusion — CI
        Summary must render FAILURE, never SUCCESS (green-by-skip) and never
        silently absent."""
        jobs = [
            {**j, "conclusion": "skipped"} if j["name"] == _JOB_NAME else j
            for j in self._healthy_jobs()
        ]
        code, report = evaluate(jobs)
        assert code == EXIT_FAILURE, report
        assert code != EXIT_SUCCESS

    def test_draft_pr_absent_coverage_sweep_gate_is_never_success(self) -> None:
        """Before the job even instantiates a `skipped` check-run (the
        earliest poll cycles of a draft PR's run), the gate is simply
        ABSENT. This must be PENDING (poll again), never a premature
        SUCCESS."""
        jobs = [j for j in self._healthy_jobs() if j["name"] != _JOB_NAME]
        code, report = evaluate(jobs)
        assert code != EXIT_SUCCESS, report
