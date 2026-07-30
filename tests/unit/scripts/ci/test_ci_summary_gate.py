# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the ``CI Summary`` fail-closed poller gate (OMN-14127 fan-out).

These pin the DEFAULT-DENY / FAIL-CLOSED verdict policy that lets ``omnimarket``
drop the ``needs``-gated ``ci-summary`` (which went absent under fleet
saturation and wedged PRs forever) for a NO-``needs`` poller that always renders
a terminal state.

The load-bearing G2 proof is
``test_never_terminalizing_gate_is_pending_never_absent_or_pass``: a seeded gate
job that never terminalizes yields PENDING (poll again) → converted to FAILURE
at the caller's deadline — never SUCCESS and never absence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.ci.ci_summary_gate import (
    EXIT_FAILURE,
    EXIT_PENDING,
    EXIT_SUCCESS,
    SELF_JOB_NAME,
    SKIPPABLE_GATE_JOBS,
    SOFT_ALLOWLIST,
    STRICT_GATE_JOBS,
    UNSUMMARIZED_REQUIRED_CONTEXTS,
    evaluate,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

pytestmark = pytest.mark.unit


def _job(
    name: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    run_attempt: int = 1,
) -> dict[str, object]:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": run_attempt,
    }


def _healthy_jobs() -> list[dict[str, object]]:
    """A fully-passing snapshot: every gate completed+success, matrix complete."""

    jobs: list[dict[str, object]] = []
    for name in STRICT_GATE_JOBS:
        jobs.append(_job(name, conclusion="success"))
    for name in SKIPPABLE_GATE_JOBS:
        jobs.append(_job(name, conclusion="success"))
    # Dynamic test matrix: detect-changes succeeded (it is a skippable gate,
    # added above), so >= 1 split must exist and be completed.
    jobs.append(_job("Tests (Split 1/1)", conclusion="success"))
    # The poller's own job, still running while it evaluates.
    jobs.append(_job(SELF_JOB_NAME, status="in_progress", conclusion=None))
    return jobs


# --------------------------------------------------------------------------- #
# Baseline: a fully-healthy snapshot passes.
# --------------------------------------------------------------------------- #


def test_healthy_snapshot_is_success() -> None:
    code, report = evaluate(_healthy_jobs())
    assert code == EXIT_SUCCESS, report


# --------------------------------------------------------------------------- #
# G2 PROOF: never-terminalizing / absent gate => never absent, never a pass.
# --------------------------------------------------------------------------- #


def test_never_terminalizing_gate_is_pending_never_absent_or_pass() -> None:
    """A gate stuck ``in_progress`` forever must be PENDING, never SUCCESS.

    This is the exact fleet-saturation shape: a required gate job that never
    reaches a terminal state. The old needs-based CI Summary went ABSENT here
    (no check-run at all) and wedged the PR. The poller instead keeps returning
    PENDING (exit 2), which the caller converts to FAILURE at the deadline —
    fail-closed, and the required context always posts a terminal state.
    """

    jobs = _healthy_jobs()
    # Seed: force one strict gate to hang forever.
    hung = STRICT_GATE_JOBS[0]
    jobs = [
        _job(hung, status="in_progress", conclusion=None) if j["name"] == hung else j
        for j in jobs
    ]
    code, report = evaluate(jobs)
    assert code == EXIT_PENDING, report
    assert code != EXIT_SUCCESS


def test_absent_gate_is_pending_never_success() -> None:
    """A gate that has not appeared yet must be PENDING, never SUCCESS."""

    absent = STRICT_GATE_JOBS[3]
    jobs = [j for j in _healthy_jobs() if j["name"] != absent]
    code, report = evaluate(jobs)
    assert code == EXIT_PENDING, report


def test_empty_snapshot_is_pending_not_success() -> None:
    """An empty job list (nothing instantiated yet) must never be SUCCESS."""

    code, _ = evaluate([])
    assert code == EXIT_PENDING


# --------------------------------------------------------------------------- #
# Strict vs skippable semantics.
# --------------------------------------------------------------------------- #


def test_strict_gate_skipped_fails_closed() -> None:
    """A skipped STRICT gate = un-enforced = FAILURE (fail-on-skipped)."""

    target = STRICT_GATE_JOBS[5]
    jobs = [
        _job(target, conclusion="skipped") if j["name"] == target else j
        for j in _healthy_jobs()
    ]
    code, report = evaluate(jobs)
    assert code == EXIT_FAILURE, report


def test_strict_gate_failure_fails() -> None:
    target = STRICT_GATE_JOBS[2]
    jobs = [
        _job(target, conclusion="failure") if j["name"] == target else j
        for j in _healthy_jobs()
    ]
    code, _ = evaluate(jobs)
    assert code == EXIT_FAILURE


def test_occ_companion_merged_gate_is_strict_and_fails_closed() -> None:
    """OMN-15427: the companion-merged gate must BLOCK, not merely report.

    omnimarket#1953 carried a CLOSED-unmerged OCC companion citation and no
    omnimarket surface caught it. Detection without enforcement is the failure
    mode being closed, so this pins the enforcement wiring itself: the gate is
    a STRICT gate (a red/skipped/cancelled conclusion fails the required
    ``CI Summary`` context) and its absence is PENDING, never a vacuous green.
    """

    gate = "OCC Companion Merged Gate (OMN-15214)"
    assert gate in STRICT_GATE_JOBS
    assert gate not in SKIPPABLE_GATE_JOBS

    # Red → FAILURE, and the gate is named in the report.
    jobs = [
        _job(gate, conclusion="failure") if j["name"] == gate else j
        for j in _healthy_jobs()
    ]
    code, report = evaluate(jobs)
    assert code == EXIT_FAILURE, report
    assert gate in report

    # Skipped → FAILURE (the job is unconditional in ci.yml; a skip is anomalous).
    jobs = [
        _job(gate, conclusion="skipped") if j["name"] == gate else j
        for j in _healthy_jobs()
    ]
    code, _ = evaluate(jobs)
    assert code == EXIT_FAILURE

    # Absent entirely → PENDING (completeness anchor), never SUCCESS.
    jobs = [j for j in _healthy_jobs() if j["name"] != gate]
    code, _ = evaluate(jobs)
    assert code == EXIT_PENDING


def test_merge_hold_gate_is_strict_and_fails_closed() -> None:
    """OMN-15483: the hold gate must BLOCK, and must not be skippable.

    This registration is the entire mechanism for the controller half of the
    ticket. The merge NODE honoring the hold marker binds one consumer; the
    foreground Codex controller that performed every merge in OMN-15483's
    incident table contains no omnimarket code and cannot be bound by any
    amount of it. Enforcement therefore lives at the surface every consumer
    already respects — required status checks — and THIS strict slot is what
    makes it required. If the gate were merely present in ci.yml but not
    registered here, a held PR would still go required-green and every
    consumer would still land it: detection without enforcement.
    """

    gate = "Merge Hold Gate (OMN-15483)"
    assert gate in STRICT_GATE_JOBS
    assert gate not in SKIPPABLE_GATE_JOBS

    # Held PR (gate red) → FAILURE, and the gate is named in the report.
    jobs = [
        _job(gate, conclusion="failure") if j["name"] == gate else j
        for j in _healthy_jobs()
    ]
    code, report = evaluate(jobs)
    assert code == EXIT_FAILURE, report
    assert gate in report

    # Skipped → FAILURE. The job is unconditional in ci.yml (no needs/if), so a
    # skip is anomalous — and "skip the hold gate" is the obvious bypass.
    jobs = [
        _job(gate, conclusion="skipped") if j["name"] == gate else j
        for j in _healthy_jobs()
    ]
    code, _ = evaluate(jobs)
    assert code == EXIT_FAILURE

    # Absent entirely → PENDING (completeness anchor), never SUCCESS.
    jobs = [j for j in _healthy_jobs() if j["name"] != gate]
    code, _ = evaluate(jobs)
    assert code == EXIT_PENDING


def test_skippable_gate_skipped_is_success() -> None:
    """A SKIPPABLE gate may legitimately skip (docs-only PR) and still pass."""

    target = SKIPPABLE_GATE_JOBS[2]  # not Detect Changes (which drives the matrix)
    assert target != "Detect Changes"
    jobs = [
        _job(target, conclusion="skipped") if j["name"] == target else j
        for j in _healthy_jobs()
    ]
    code, report = evaluate(jobs)
    assert code == EXIT_SUCCESS, report


def test_skippable_gate_failure_fails() -> None:
    target = SKIPPABLE_GATE_JOBS[2]
    jobs = [
        _job(target, conclusion="failure") if j["name"] == target else j
        for j in _healthy_jobs()
    ]
    code, _ = evaluate(jobs)
    assert code == EXIT_FAILURE


# --------------------------------------------------------------------------- #
# Default-deny sweep.
# --------------------------------------------------------------------------- #


def test_unlisted_failed_job_fails_via_sweep() -> None:
    """A failing job outside every gate list must still fail the summary."""

    jobs = _healthy_jobs()
    jobs.append(_job("Some Brand New Gate", conclusion="failure"))
    code, report = evaluate(jobs)
    assert code == EXIT_FAILURE, report


def test_allowlisted_shadow_failure_is_tolerated() -> None:
    """An allowlisted advisory/shadow job may fail without failing the gate."""

    allowlisted = next(iter(SOFT_ALLOWLIST))
    jobs = _healthy_jobs()
    jobs.append(_job(allowlisted, conclusion="failure"))
    code, report = evaluate(jobs)
    assert code == EXIT_SUCCESS, report


def test_self_failure_does_not_self_fail() -> None:
    """A failed ``CI Summary`` row (prior attempt residue) must not self-fail."""

    jobs = _healthy_jobs()
    jobs.append(_job(SELF_JOB_NAME, status="completed", conclusion="failure"))
    # dedup keeps the most-blocking same-attempt row (the completed failure),
    # but the poller's own job is excluded from every check, so it is ignored.
    code, report = evaluate(jobs)
    assert code == EXIT_SUCCESS, report


# --------------------------------------------------------------------------- #
# Test-matrix completeness.
# --------------------------------------------------------------------------- #


def test_failed_split_fails_via_sweep() -> None:
    jobs = _healthy_jobs()
    jobs.append(_job("Tests (Split 2/2)", conclusion="failure"))
    code, report = evaluate(jobs)
    assert code == EXIT_FAILURE, report


def test_detect_changes_success_but_no_splits_is_pending() -> None:
    """Splits are created async after detect-changes; zero present => PENDING."""

    jobs = [
        j for j in _healthy_jobs() if not str(j["name"]).startswith("Tests (Split ")
    ]
    code, report = evaluate(jobs)
    assert code == EXIT_PENDING, report


def test_split_in_progress_is_pending() -> None:
    jobs = _healthy_jobs()
    jobs.append(_job("Tests (Split 2/2)", status="in_progress", conclusion=None))
    code, report = evaluate(jobs)
    assert code == EXIT_PENDING, report


def test_docs_only_pr_waives_matrix() -> None:
    """detect-changes skipped (docs-only) => the matrix is waived, gate passes."""

    jobs: list[dict[str, object]] = []
    for name in STRICT_GATE_JOBS:
        jobs.append(_job(name, conclusion="success"))
    for name in SKIPPABLE_GATE_JOBS:
        # docs-only: the whole docs-gated lane (incl. Detect Changes) skips.
        jobs.append(_job(name, conclusion="skipped"))
    jobs.append(_job(SELF_JOB_NAME, status="in_progress", conclusion=None))
    # No "Tests (Split …)" jobs exist at all on a docs-only PR.
    code, report = evaluate(jobs)
    assert code == EXIT_SUCCESS, report


# --------------------------------------------------------------------------- #
# run_attempt scoping (rerun hygiene).
# --------------------------------------------------------------------------- #


def test_run_attempt_scopes_out_stale_failed_row() -> None:
    """A failed row from attempt 1 must not fail an attempt-2 evaluation."""

    jobs = _healthy_jobs()
    for j in jobs:
        j["run_attempt"] = 2
    # Stale attempt-1 failure of a strict gate.
    jobs.append(_job(STRICT_GATE_JOBS[0], conclusion="failure", run_attempt=1))
    code, report = evaluate(jobs, run_attempt=2)
    assert code == EXIT_SUCCESS, report


# --------------------------------------------------------------------------- #
# Strict-list derivation is pinned to the real workflow + coverage honesty.
# --------------------------------------------------------------------------- #


def _ci_jobs() -> dict[str, dict]:
    return yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]


def test_every_gate_name_maps_to_a_real_ci_job() -> None:
    """Each strict/skippable gate display name must resolve to a ci.yml job.

    This pins the anchor to the actual workflow: a job rename that would
    silently drop a gate out of the poller's coverage breaks this test instead.
    """

    jobs = _ci_jobs()
    display_names: set[str] = set(jobs.keys())
    for defn in jobs.values():
        if isinstance(defn, dict) and defn.get("name"):
            display_names.add(str(defn["name"]))

    for gate in (*STRICT_GATE_JOBS, *SKIPPABLE_GATE_JOBS):
        if " / " in gate:
            # Reusable-workflow caller: "<job key> / <inner job name>".
            caller = gate.split(" / ", 1)[0]
            assert caller in jobs, (
                f"gate {gate!r}: caller job {caller!r} is not a ci.yml job"
            )
        else:
            assert gate in display_names, (
                f"gate {gate!r} does not match any ci.yml job name or key"
            )


def test_strict_and_skippable_are_disjoint() -> None:
    assert not (set(STRICT_GATE_JOBS) & set(SKIPPABLE_GATE_JOBS))


def test_coverage_gap_is_disclosed_and_disjoint() -> None:
    """The unsummarized required contexts must be non-empty and not overlap gates.

    Coverage honesty: CI Summary summarizes ci.yml jobs only. The unsummarized
    set names the independently-required contexts from OTHER workflow files so
    the name never implies whole-PR coverage.
    """

    assert UNSUMMARIZED_REQUIRED_CONTEXTS, "coverage gap must be disclosed"
    summarized = set(STRICT_GATE_JOBS) | set(SKIPPABLE_GATE_JOBS)
    overlap = summarized & set(UNSUMMARIZED_REQUIRED_CONTEXTS)
    assert not overlap, f"unsummarized set must be disjoint from gates: {overlap}"


def test_report_discloses_coverage_gap() -> None:
    _, report = evaluate(_healthy_jobs())
    assert "NOT summarized" in report
    assert "UNSUMMARIZED_REQUIRED_CONTEXTS" in report
