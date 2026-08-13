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
    ACTOR_CONDITIONAL_CONTEXTS,
    EXIT_FAILURE,
    EXIT_PENDING,
    EXIT_SUCCESS,
    EXPECTED_EXTERNAL_CONTEXTS,
    EXTERNAL_GOOD_CONCLUSIONS,
    SELF_JOB_NAME,
    SKIPPABLE_GATE_JOBS,
    SOFT_ALLOWLIST,
    STRICT_GATE_JOBS,
    dedup_latest_check_runs,
    evaluate,
    evaluate_external,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS_DIR / "ci.yml"

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


def _ci_summary_gate_invocations() -> list[str]:
    """Every ``ci_summary_gate.py`` command line inside ci.yml's ci-summary job."""

    job = _ci_jobs()["ci-summary"]
    lines: list[str] = []
    for step in job.get("steps", []):
        for line in str(step.get("run", "")).splitlines():
            stripped = line.strip()
            # Only real invocations — not the prose comments that name the file.
            if stripped.startswith("python3 ") and "ci_summary_gate.py" in stripped:
                lines.append(stripped)
    return lines


def test_ci_yml_wires_l4_check_runs_file_and_actor() -> None:
    """L4 is OPT-IN at the CLI: ``--check-runs-file`` defaults to ``None``, and
    ``main()`` skips the entire external-context layer when it is absent
    (exit code then reflects layers 1-3 only). That makes ci.yml's invocation
    the single load-bearing wire for all
    ``EXPECTED_EXTERNAL_CONTEXTS`` assertions — dropping the flag in a refactor
    would silently revert this gate to its pre-L4 behavior with every unit test
    still green. This pins the wire so that regression fails here instead.
    """

    invocations = _ci_summary_gate_invocations()
    assert invocations, "ci-summary job invokes ci_summary_gate.py nowhere"

    for cmd in invocations:
        assert "--check-runs-file" in cmd, (
            "ci-summary invocation omits --check-runs-file; L4 "
            f"({len(EXPECTED_EXTERNAL_CONTEXTS)} external contexts) would "
            f"silently no-op: {cmd!r}"
        )
        assert "--actor" in cmd, (
            "ci-summary invocation omits --actor; ACTOR_CONDITIONAL_CONTEXTS "
            f"cannot be resolved and would gap for every actor: {cmd!r}"
        )


def test_ci_summary_job_can_read_check_runs() -> None:
    """The L4 fetch needs ``checks: read``. Without it the ``gh api`` call 403s,
    ci.yml's ``|| echo "[]"`` fallback writes an empty array, and every external
    context reads as unobserved — a permanent PENDING->FAILURE wedge rather
    than a working gate. Pin the permission that keeps L4 evaluable.
    """

    permissions = _ci_jobs()["ci-summary"].get("permissions") or {}
    assert permissions.get("checks") == "read", (
        f"ci-summary job needs `checks: read` for the L4 "
        f"commits/{{sha}}/check-runs fetch; got {permissions!r}"
    )


def test_ci_summary_l4_uses_pr_head_sha_not_merge_sha() -> None:
    """``commits/{sha}/check-runs`` is keyed by commit. On ``pull_request`` the
    run's own ``github.sha`` is the ephemeral merge commit, which carries none
    of the other workflows' check-runs — resolving L4 against it would make
    all external contexts permanently missing. Pin that HEAD_SHA prefers the
    PR head.
    """

    job = _ci_jobs()["ci-summary"]
    head_sha_exprs = [
        str((step.get("env") or {}).get("HEAD_SHA", ""))
        for step in job.get("steps", [])
        if (step.get("env") or {}).get("HEAD_SHA")
    ]
    assert head_sha_exprs, "ci-summary defines no HEAD_SHA for the L4 fetch"
    for expr in head_sha_exprs:
        assert "pull_request.head.sha" in expr, (
            "HEAD_SHA must resolve to the PR head SHA on pull_request events, "
            f"not the merge commit: {expr!r}"
        )


def test_external_contexts_are_disclosed_and_disjoint_from_in_run_gates() -> None:
    """L4 EXPECTED_EXTERNAL_CONTEXTS must be non-empty and not overlap L1/L2.

    A context produced INSIDE ci.yml's own run belongs in STRICT/SKIPPABLE
    (L1/L2), not in the L4 external-context tuple — the two layers read
    different APIs (in-run jobs vs. commits/{sha}/check-runs) and must not
    double-count the same producer.
    """

    assert EXPECTED_EXTERNAL_CONTEXTS, "L4 external-context set must be non-empty"
    in_run = set(STRICT_GATE_JOBS) | set(SKIPPABLE_GATE_JOBS)
    overlap = in_run & set(EXPECTED_EXTERNAL_CONTEXTS)
    assert not overlap, f"L4 must be disjoint from in-run gates: {overlap}"


def test_report_discloses_external_context_layer() -> None:
    _, report = evaluate(_healthy_jobs())
    assert "L4 EXPECTED_EXTERNAL_CONTEXTS" in report
    assert str(len(EXPECTED_EXTERNAL_CONTEXTS)) in report


# --------------------------------------------------------------------------- #
# L4: external-context layer (other workflow files via commits/{sha}/check-runs).
# --------------------------------------------------------------------------- #


def _check_run(
    name: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    started_at: str = "2026-08-13T00:00:00Z",
) -> dict[str, object]:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": started_at,
    }


def _healthy_check_runs() -> list[dict[str, object]]:
    return [_check_run(name) for name in EXPECTED_EXTERNAL_CONTEXTS]


def test_external_all_present_success_is_success() -> None:
    code, report = evaluate_external(_healthy_check_runs())
    assert code == EXIT_SUCCESS, report


def test_external_unfetchable_check_runs_is_pending_never_success() -> None:
    """``check_runs is None`` (fetch failed) must be PENDING, never a blind pass."""

    code, report = evaluate_external(None)
    assert code == EXIT_PENDING, report
    assert code != EXIT_SUCCESS


def test_external_context_missing_is_pending_never_success() -> None:
    target = EXPECTED_EXTERNAL_CONTEXTS[0]
    runs = [r for r in _healthy_check_runs() if r["name"] != target]
    code, report = evaluate_external(runs)
    assert code == EXIT_PENDING, report
    assert target in report


def test_external_context_skipped_fails_closed() -> None:
    """L4 has no re-derived skip precondition: 'skipped' fails closed here,
    unlike the in-run L2 SKIPPABLE_GATE_JOBS treatment."""

    target = EXPECTED_EXTERNAL_CONTEXTS[1]
    runs = [
        _check_run(target, conclusion="skipped") if r["name"] == target else r
        for r in _healthy_check_runs()
    ]
    code, report = evaluate_external(runs)
    assert code == EXIT_FAILURE, report


def test_external_context_failure_fails_closed() -> None:
    target = EXPECTED_EXTERNAL_CONTEXTS[2]
    runs = [
        _check_run(target, conclusion="failure") if r["name"] == target else r
        for r in _healthy_check_runs()
    ]
    code, report = evaluate_external(runs)
    assert code == EXIT_FAILURE, report


def test_external_context_in_progress_is_pending() -> None:
    target = EXPECTED_EXTERNAL_CONTEXTS[3]
    runs = [
        _check_run(target, status="in_progress", conclusion=None)
        if r["name"] == target
        else r
        for r in _healthy_check_runs()
    ]
    code, report = evaluate_external(runs)
    assert code == EXIT_PENDING, report


def test_external_good_conclusions_is_success_only() -> None:
    """L4's good-conclusion set must be strictly {'success'} — no leniency."""

    assert frozenset({"success"}) == EXTERNAL_GOOD_CONCLUSIONS


def test_actor_conditional_context_legitimately_absent_for_named_actor() -> None:
    """dependabot[bot] never gets a 'gate / CodeRabbit Thread Check' check-run
    (producer-side `if:` in cr-thread-gate-caller.yml) — this must NOT count
    as a gap for that one actor, but must still gap for anyone else."""

    context = "gate / CodeRabbit Thread Check"
    assert context in ACTOR_CONDITIONAL_CONTEXTS
    runs = [r for r in _healthy_check_runs() if r["name"] != context]

    code, report = evaluate_external(runs, actor="dependabot[bot]")
    assert code == EXIT_SUCCESS, report

    code2, report2 = evaluate_external(runs, actor="some-human")
    assert code2 == EXIT_PENDING, report2
    assert context in report2


def test_falsification_control_each_external_entry_is_load_bearing() -> None:
    """Deleting any one EXPECTED_EXTERNAL_CONTEXTS entry from the asserted set
    must flip a fixture with that context RED from FAILURE to SUCCESS —
    proving every entry actually participates in the verdict, not just sits
    in the tuple unused."""

    for missing_context in EXPECTED_EXTERNAL_CONTEXTS:
        runs = [
            _check_run(missing_context, conclusion="failure")
            if r["name"] == missing_context
            else r
            for r in _healthy_check_runs()
        ]
        pruned = tuple(n for n in EXPECTED_EXTERNAL_CONTEXTS if n != missing_context)

        code_with, _ = evaluate_external(runs, expected=EXPECTED_EXTERNAL_CONTEXTS)
        assert code_with == EXIT_FAILURE, missing_context

        code_without, _ = evaluate_external(runs, expected=pruned)
        assert code_without == EXIT_SUCCESS, (
            f"{missing_context!r} is not load-bearing: removing it from the "
            "asserted set did not flip a red fixture to green"
        )


def test_dedup_latest_check_runs_keeps_most_recent_row() -> None:
    runs = [
        _check_run("X", conclusion="success", started_at="2026-08-13T00:00:00Z"),
        _check_run("X", conclusion="failure", started_at="2026-08-13T00:05:00Z"),
    ]
    latest = dedup_latest_check_runs(runs)
    assert latest["X"].conclusion == "failure"


def test_dedup_latest_check_runs_tie_breaks_toward_more_blocking() -> None:
    runs = [
        _check_run("Y", conclusion="success", started_at="2026-08-13T00:00:00Z"),
        _check_run("Y", conclusion="failure", started_at="2026-08-13T00:00:00Z"),
    ]
    latest = dedup_latest_check_runs(runs)
    assert latest["Y"].conclusion == "failure"


# --------------------------------------------------------------------------- #
# RED-REPLAY: PR #1968-shaped fixture — deploy-gate red, everything else green.
# --------------------------------------------------------------------------- #


def test_red_replay_deploy_gate_red_now_fails() -> None:
    """omnimarket#1968 (2026-07-30): merged with 'CI Summary' = success while
    'deploy-gate / deploy-gate' = failure on the same head SHA, because
    deploy-gate was not required on dev, directly or transitively — the
    in-run poller never saw it (separate workflow file, separate run) and
    branch protection didn't require it there either. This fixture pins that
    the same shape now evaluates FAILURE via the L4 layer alone."""

    assert "deploy-gate / deploy-gate" in EXPECTED_EXTERNAL_CONTEXTS
    runs = [
        _check_run("deploy-gate / deploy-gate", conclusion="failure")
        if r["name"] == "deploy-gate / deploy-gate"
        else r
        for r in _healthy_check_runs()
    ]
    code, report = evaluate_external(runs)
    assert code == EXIT_FAILURE, report
    assert "deploy-gate / deploy-gate" in report


# --------------------------------------------------------------------------- #
# COMPLETENESS TEST — the real deliverable (doctrine: "no enumeration of what
# is not enforced; the only way a job escapes assertion is an explicit
# code-level exemption entry with a one-line reason").
#
# Parses EVERY .github/workflows/*.yml, derives every job reachable from an
# `on.pull_request` trigger that can target `dev` (a `branches:` list
# containing "dev", or no `branches:` filter at all — which fires for every
# base branch), and asserts that set == STRICT | SKIPPABLE | EXTERNAL |
# EXEMPT. A newly added, unclassified workflow job fails this test until it
# is triaged into one of the four buckets — that is what mechanically keeps
# "enforce everything" flipped after this PR merges.
# --------------------------------------------------------------------------- #

# EXEMPT: (workflow filename, job key) -> one-line reason. Every entry here is
# a job that CAN be triggered by a pull_request targeting dev/main but is
# deliberately NOT asserted (not STRICT, not SKIPPABLE, not in
# EXPECTED_EXTERNAL_CONTEXTS). `continue-on-error: true` / `|| true` on an
# enforcing step is NOT a valid reason to land here — every reason below is
# either a self-declared non-validator (automation/observer/post-merge), a
# self-declared staged/deferred promotion, or an explicitly time-boxed
# deferral with a stated follow-up condition.
EXEMPT_CONTEXTS: dict[tuple[str, str], str] = {
    # --- duplicate-name internal "wait for occ-preflight / eligibility"
    # precondition jobs. Each produces a check-run literally named "OCC
    # Preflight Dependency" (same display name as ci.yml's own STRICT gate),
    # but it is a *local* precondition for that one workflow's own next job,
    # not an independently required context — the actual required gate is
    # "occ-preflight / eligibility" (L4, asserted).
    ("auto-merge.yml", "occ-preflight"): (
        "internal precondition-wait job (blocks this workflow's own "
        "auto-merge job until occ-preflight / eligibility posts); not "
        "independently required — occ-preflight / eligibility is the real "
        "gate and IS asserted (L4)."
    ),
    ("dep-health-gate.yml", "occ-preflight"): (
        "internal precondition-wait job (blocks this workflow's own "
        "dep-health job); not independently required — occ-preflight / "
        "eligibility is the real gate and IS asserted (L4)."
    ),
    ("market-skill-baseline.yml", "occ-preflight"): (
        "internal precondition-wait job; not independently required — "
        "occ-preflight / eligibility is the real gate and IS asserted (L4)."
    ),
    ("plugin-compat-gate.yml", "occ-preflight"): (
        "internal precondition-wait job; not independently required — "
        "occ-preflight / eligibility is the real gate and IS asserted (L4)."
    ),
    ("validator-runtime-profiles.yml", "occ-preflight"): (
        "internal precondition-wait job; not independently required — "
        "occ-preflight / eligibility is the real gate and IS asserted (L4)."
    ),
    # --- automation / observer workflows: self-declared non-blocking, or
    # structurally incapable of gating (post-merge triggers).
    ("auto-merge.yml", "auto-merge"): (
        "auto-merge enabler automation (arms GH auto-merge on pull_request "
        "opened), not a PR content validator — does not itself gate the "
        "merge decision."
    ),
    ("auto-tag-on-merge.yml", "auto-tag"): (
        "post-merge automation (if: pull_request.merged == true) — "
        "structurally cannot gate the merge that already happened."
    ),
    ("call-occ-attestation-observe.yml", "occ-attestation-observe"): (
        "self-declared report-only, non-blocking observer (job name: "
        "'OCC Attestation Observe (report-only, non-blocking)')."
    ),
    ("call-occ-autobind.yml", "publish-occ-autobind"): (
        "additive command publisher, never fails a PR — same contract as "
        "the omniweb/omnibase_infra call-occ-autobind siblings."
    ),
    ("call-occ-companion-author.yml", "occ-companion-author"): (
        "OCC companion authoring automation (dry_run by default per "
        "OMNI_OCC_AUTOAUTHOR_MODE), not a PR content validator."
    ),
    ("call-occ-companion-observe.yml", "occ-companion-observe"): (
        "self-declared dry_run, non-blocking observer (job name: "
        "'OCC Companion Observe (dry_run, non-blocking)')."
    ),
    ("call-occ-preflight.yml", "governance-readiness"): (
        "self-declared REPORT-ONLY shadow (WS3/OMN-14646); its own inline "
        "comment states it is 'WITHOUT being added to branch-protection "
        "required_status_checks' by design."
    ),
    ("pr-merged-publisher.yml", "publish-pr-merged"): (
        "post-merge automation (if: pull_request.merged == true) — "
        "structurally cannot gate the merge that already happened."
    ),
    ("todo-audit-on-merge.yml", "todo-audit"): (
        "post-merge automation (if: pull_request.merged == true) — "
        "structurally cannot gate the merge that already happened."
    ),
    # --- self-declared staged / deliberately-deferred promotion.
    ("dep-health-gate.yml", "dep-health"): (
        "Phase 1 advisory by explicit design (file header: 'Phase 1 "
        "(advisory): no baseline file -> run sweep, upload artifact, exit "
        "0'); Phase 2 blocking activates automatically once "
        ".onex_state/dep_health_baseline.json is committed — no separate "
        "promotion PR needed then; out of this wave's scope."
    ),
    ("receipt-honesty.yml", "receipt-honesty"): (
        "self-declared staged, not yet promoted (file header: "
        "'Required-status-check name (when later flipped)')."
    ),
    ("shellcheck-gate.yml", "shell-hygiene"): (
        "self-declared 'can be promoted to a required status check' — "
        "promotion deliberately deferred."
    ),
    ("product-readiness-shadow.yml", "lint-shadow"): (
        "Phase-3 canary, self-declared 'ENFORCING BUT NON-REQUIRED' — "
        "cutover explicitly gated on a zero-disagreement window + "
        "maintainer approval."
    ),
    ("product-readiness-shadow.yml", "typecheck-shadow"): (
        "Phase-3 canary, self-declared 'ENFORCING BUT NON-REQUIRED' — "
        "cutover explicitly gated on a zero-disagreement window + "
        "maintainer approval."
    ),
    ("product-readiness-shadow.yml", "tests-shadow"): (
        "Phase-3 canary, self-declared 'ENFORCING BUT NON-REQUIRED' — "
        "cutover explicitly gated on a zero-disagreement window + "
        "maintainer approval."
    ),
    ("product-readiness-shadow.yml", "product-readiness"): (
        "Phase-3 canary, self-declared 'ENFORCING BUT NON-REQUIRED' — "
        "cutover explicitly gated on a zero-disagreement window + "
        "maintainer approval."
    ),
    ("product-readiness-shadow.yml", "reason-graph"): (
        "Phase-3 canary, self-declared 'ENFORCING BUT NON-REQUIRED'; "
        "removed from omnimarket dev branch protection 2026-07-25 — "
        "cutover explicitly gated on a zero-disagreement window + "
        "maintainer approval."
    ),
    # --- path-filtered validators, explicitly out of this wave's scope (not
    # named in the enforce-everything build-order plan for omnimarket).
    # Promotion requires the two-step order-of-operations sequence (convert
    # paths: to always-fire + in-job short-circuit, merge, observe one green
    # PR cycle, THEN assert) — promoting an unproven reporter straight to L4
    # is the documented wedge trap.
    ("market-skill-baseline.yml", "market-skill-baseline"): (
        "path-filtered validator; promotion deferred pending the "
        "path-filter->always-fire conversion + one observed proving PR "
        "cycle; not in this wave's scope."
    ),
    ("plugin-compat-gate.yml", "plugin-compat-handshake"): (
        "path-filtered validator; promotion deferred pending the "
        "path-filter->always-fire conversion + one observed proving PR "
        "cycle; not in this wave's scope."
    ),
    # --- security-scan.yml: pull_request trigger newly added by this PR.
    # Unproven PR-event reporters — never posted a check-run on a
    # pull_request event before. Promoting an unproven reporter straight to
    # L4 is the documented wedge trap; deferred pending one observed green
    # run on a real PR.
    ("security-scan.yml", "dependency-scan"): (
        "pull_request trigger newly added by this PR; unproven PR-event "
        "reporter — promotion to L4 deferred pending one observed green "
        "run on a real PR (order-of-operations rule); follow-up required."
    ),
    ("security-scan.yml", "secret-scan"): (
        "pull_request trigger newly added by this PR; unproven PR-event "
        "reporter — promotion to L4 deferred pending one observed green "
        "run on a real PR (order-of-operations rule); follow-up required."
    ),
    ("security-scan.yml", "codeql"): (
        "pull_request trigger newly added by this PR; unproven PR-event "
        "reporter — promotion to L4 deferred pending one observed green "
        "run on a real PR (order-of-operations rule); follow-up required."
    ),
}

# ci.yml jobs covered by the L1/L2/L3 architecture without an individual
# named entry in STRICT_GATE_JOBS/SKIPPABLE_GATE_JOBS: the dynamic test
# matrix, the SOFT_ALLOWLIST shadow job, the reusable merge-hold-gate caller
# (covered by the L3 default-deny sweep — no legitimate skip path, so any
# non-good conclusion still fails the gate even though it isn't individually
# named), and the poller's own self-referential job.
_CI_YML_STRUCTURALLY_COVERED_KEYS: frozenset[str] = frozenset(
    {
        "test",  # dynamic "Tests (Split N/M)" matrix — see the matrix rule.
        "merge-hold-gate",  # reusable caller; covered by the L3 default-deny sweep.
        "ci-summary",  # the poller's own job (SELF_JOB_NAME).
    }
)


def _yaml_on_block(doc: dict) -> dict | list | None:
    # PyYAML's default (non-1.2) resolver parses the bare `on:` mapping key as
    # the boolean `True`, not the string "on".
    if True in doc:
        return doc[True]
    return doc.get("on")


def _pull_request_targets_dev(pr_cfg: object) -> bool:
    """True if this pull_request trigger can fire against `dev` (or is
    unrestricted, which fires against every base branch including dev)."""

    if pr_cfg is None:
        return False
    branches = pr_cfg.get("branches") if isinstance(pr_cfg, dict) else None
    if branches is None:
        return True
    return "dev" in branches


def _iter_pr_triggered_jobs() -> list[tuple[str, str, str]]:
    """Yield (workflow_filename, job_key, display_name) for every job
    reachable from a `pull_request` trigger targeting `dev` across every
    workflow file in this repo."""

    found: list[tuple[str, str, str]] = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        if not isinstance(doc, dict):
            continue
        on_block = _yaml_on_block(doc)
        if not isinstance(on_block, dict):
            continue
        pr_cfg = on_block.get("pull_request")
        if pr_cfg is None or not _pull_request_targets_dev(pr_cfg):
            continue
        jobs = doc.get("jobs", {})
        if not isinstance(jobs, dict):
            continue
        for job_key, job_def in jobs.items():
            display = job_key
            if isinstance(job_def, dict) and job_def.get("name"):
                display = str(job_def["name"])
            found.append((path.name, str(job_key), str(display)))
    return found


def _matches_target(
    job_key: str, display: str, targets: frozenset[str] | set[str]
) -> bool:
    """True if ``display`` is directly in ``targets``, OR ``job_key`` is the
    caller segment of a compound "<job_key> / <inner job name>" entry.

    A reusable-workflow caller job (``uses:``) surfaces in the GitHub check-
    runs / jobs API as ``"<job key> / <inner job name>"`` — the caller job's
    own ``name:`` field (if any) is not what shows up. Static YAML parsing
    cannot resolve the inner job name for a CROSS-REPO reusable (it lives in
    another repo's workflow file), so this mirrors the same caller-prefix
    matching :func:`scripts.ci.ci_summary_gate._is_allowlisted` already uses
    at runtime for exactly this shape.
    """

    if display in targets:
        return True
    prefix = f"{job_key} / "
    return any(t.startswith(prefix) for t in targets)


def test_every_pr_triggered_job_is_classified() -> None:
    """The completeness anchor: every job reachable from `pull_request` (to
    dev) across every workflow file must resolve to STRICT, SKIPPABLE,
    EXTERNAL, or EXEMPT. A new, unclassified workflow job fails this test."""

    external = set(EXPECTED_EXTERNAL_CONTEXTS)
    ci_yml_targets = (
        set(STRICT_GATE_JOBS) | set(SKIPPABLE_GATE_JOBS) | set(SOFT_ALLOWLIST)
    )
    unclassified: list[str] = []

    for filename, job_key, display in _iter_pr_triggered_jobs():
        if filename == "ci.yml":
            in_targets = _matches_target(job_key, display, ci_yml_targets)
            in_structural = job_key in _CI_YML_STRUCTURALLY_COVERED_KEYS
            if not (in_targets or in_structural):
                unclassified.append(
                    f"{filename}::{job_key} (name={display!r}) — not in "
                    "STRICT_GATE_JOBS, SKIPPABLE_GATE_JOBS, SOFT_ALLOWLIST, or "
                    "_CI_YML_STRUCTURALLY_COVERED_KEYS"
                )
            continue

        in_external = _matches_target(job_key, display, external)
        in_exempt = (filename, job_key) in EXEMPT_CONTEXTS
        if not (in_external or in_exempt):
            unclassified.append(
                f"{filename}::{job_key} (name={display!r}) — not in "
                "EXPECTED_EXTERNAL_CONTEXTS and not in EXEMPT_CONTEXTS"
            )

    assert not unclassified, (
        "unclassified PR-triggered job(s) found — every job reachable from "
        "pull_request must be STRICT, SKIPPABLE, EXTERNAL, or EXEMPT with a "
        "reason:\n  " + "\n  ".join(unclassified)
    )


def test_exempt_reasons_are_nonempty() -> None:
    for key, reason in EXEMPT_CONTEXTS.items():
        assert reason.strip(), f"{key} has an empty exemption reason"


def test_exempt_and_external_are_disjoint() -> None:
    """A (file, job_key) pair should not be BOTH exempt and asserted — that
    would mean the exemption reason is dead/contradicted."""

    external_names = set(EXPECTED_EXTERNAL_CONTEXTS)
    for filename, job_key in EXEMPT_CONTEXTS:
        for f, k, display in _iter_pr_triggered_jobs():
            if f == filename and k == job_key:
                assert display not in external_names, (
                    f"{filename}::{job_key} is both EXEMPT and asserted "
                    f"(display name {display!r} is in EXPECTED_EXTERNAL_CONTEXTS)"
                )


def test_no_asserted_external_workflow_has_a_pull_request_paths_filter() -> None:
    """A job that IS asserted (L4) must always produce a check-run — a
    `paths:` filter under its `pull_request` trigger would make it silently
    absent on PRs that don't touch the filtered paths, and an asserted-but-
    silently-absent context is a permanent PENDING->FAILURE wedge."""

    external = set(EXPECTED_EXTERNAL_CONTEXTS)
    offenders: list[str] = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        if not isinstance(doc, dict):
            continue
        on_block = _yaml_on_block(doc)
        if not isinstance(on_block, dict):
            continue
        pr_cfg = on_block.get("pull_request")
        if not isinstance(pr_cfg, dict) or "paths" not in pr_cfg:
            continue
        jobs = doc.get("jobs", {})
        if not isinstance(jobs, dict):
            continue
        for job_key, job_def in jobs.items():
            display = job_key
            if isinstance(job_def, dict) and job_def.get("name"):
                display = str(job_def["name"])
            if display in external:
                offenders.append(f"{path.name}::{job_key} (name={display!r})")

    assert not offenders, (
        "asserted (L4) job(s) with a pull_request `paths:` filter — convert "
        "to always-fire + in-job short-circuit before asserting:\n  "
        + "\n  ".join(offenders)
    )
