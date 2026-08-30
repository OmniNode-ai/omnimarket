# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fail-closed verdict for the ``CI Summary`` required-context poller (OMN-14127 fan-out).

Why this exists
---------------
``CI Summary`` is a required branch-protection context on ``omnimarket``'s
``dev`` and ``main`` branches. It used to be a ``needs``-gated aggregator job
(``needs: [occ-preflight, zone-filter, lint, ...28 upstream jobs]``, with a
shell loop over ``needs.<job>.result``). A ``needs``-gated job gets **no**
GitHub check-run until its ``needs`` reach a terminal state, so under
self-hosted runner-fleet saturation the gate jobs never terminalized and
``CI Summary`` was **absent** — a required context that never reports leaves the
PR wedged ``BLOCKED`` forever with no auto-recovery. This is the *exact* failure
class OMN-14127 fixed in ``omnibase_core`` (#1397), ``omnibase_infra`` (#2230),
and ``omniclaude`` (#1870); ``omnimarket`` was never ported and is the last
``needs``-gated ``ci-summary`` on the fleet. This module + the no-``needs``
poller job in ``ci.yml`` close that gap here.

The ``ci-summary`` workflow job is now a NO-``needs``, GitHub-hosted poller: its
check-run instantiates immediately (so the required context can never be
absent), and it calls this module in a loop against the current run's job list
until a terminal verdict is reached (or a bounded deadline fires → fail-closed).

Verdict policy — DEFAULT-DENY, FAIL-CLOSED
------------------------------------------
This module reproduces the *exact* strictness of the old needs-based
``ci-summary`` pass/fail loop and then adds a strictly-stronger safety net.
Four independent checks; all must be satisfied for success:

1. **Strict aggregate gates.** :data:`STRICT_GATE_JOBS` must each be *present*,
   *completed*, and conclude ``success`` — a ``skipped``/``failure``/
   ``cancelled`` conclusion fails the gate. These jobs carry **no legitimate
   ``if:`` skip path** in ``omnimarket``'s ``ci.yml`` (they are gated only on
   ``needs: occ-preflight`` and run on every ``pull_request``/``merge_group``/
   ``push``); a skip here is anomalous and fails closed. This mirrors the old
   loop's strict ``== "success"`` blocks (contract-topic-graph OMN-14640,
   no-noncanonical-lifecycle-classes OMN-14350, coverage-sweep-gate OMN-14645,
   the OMN-14590 drift trio, merge-reason-code-gate OMN-14765) *and* promotes
   the jobs the old loose loop tolerated as ``skipped`` — none of which can
   legitimately skip — to the same strict posture.

2. **Skippable aggregate gates.** :data:`SKIPPABLE_GATE_JOBS` must each be
   *present*, *completed*, and conclude ``success`` **or** ``skipped`` — these
   jobs carry a legitimate skip path in ``ci.yml``: each is gated behind
   ``needs.zone-filter.outputs.docs_only != 'true'`` (directly, or via a
   ``needs`` on ``detect-changes`` which is itself docs-only-gated), so a
   docs-only PR legitimately skips them. This matches the old loop's
   ``success || skipped`` acceptance for exactly this set.

3. **Test-matrix completeness.** The ``test`` job is a *dynamic* matrix
   (``Tests (Split N/M)``) created only after ``Detect Changes`` completes, so
   it has no stable name to anchor on. Rule: if ``Detect Changes`` is
   present+completed+``success`` (a non-docs PR), at least one
   ``Tests (Split …)`` job must exist and every present split must be completed
   — zero splits present, or any split still running, is PENDING (splits are
   created asynchronously). ``detect_test_paths`` always emits ``split_count
   >= 1`` when it runs, so "detect-changes success + zero splits present" is
   always the transient creation window, never a legitimate empty matrix. If
   ``Detect Changes`` skipped (docs-only) or is not ``success``, the matrix is
   waived here (a docs-only skip is legitimate; a ``detect-changes`` failure is
   already caught by check (2)). A *failed* split is caught by check (4).

4. **Default-deny failure sweep.** Any *other* job in the run that is *present*,
   *completed*, and whose conclusion is not ``success``/``skipped`` fails the
   gate — UNLESS it is the poller itself or one of a small, explicit
   :data:`SOFT_ALLOWLIST` of jobs that already exist in ``ci.yml`` as non-gating
   (advisory / shadow / not in the old ``ci-summary`` ``needs``). This sweep is
   what makes the poller *stricter* than the old gate: any failed ``ci.yml`` job
   not on the allowlist fails the summary, even one the old ``needs`` loop never
   listed. Failed ``Tests (Split …)`` splits are caught here too.

The strict + skippable gates together are the **completeness anchor**: requiring
them present+good proves the whole substantive matrix actually ran and passed,
which prevents a *false green* before late-created jobs have even been
instantiated. If a gate is missing or still running, the verdict is PENDING
(poll again). At the caller's deadline, PENDING is converted to FAILURE
(fail-closed): the required context always reaches a terminal state.

LAYER 4 — ``EXPECTED_EXTERNAL_CONTEXTS`` (enforce-everything gate, 2026-08)
---------------------------------------------------------------------------
``CI Summary`` polls the jobs of **its own workflow run** (``ci.yml``) via
``actions/runs/{run_id}/jobs`` — layers 1-3 above. That covers every job that
lives *inside* ``ci.yml``. It does NOT, by itself, see jobs produced by any of
omnimarket's ~60 *other* workflow files — those run in separate workflow runs
the in-run poller never observes. Until this layer existed, the poller merely
*disclosed* that gap (a hand-maintained, unenforced ``UNSUMMARIZED_REQUIRED_CONTEXTS``
doc-only list) instead of closing it — so a required context silently dropped
from branch protection (as 19 rows were on 2026-07-25) had nothing checking
that it stayed required, and a genuinely never-required validator
(fsm-handler-drift, skill-mapping-input-coverage-gate,
node-migration-vendor-parity-gate, node-drift-gate on dev, contract-validation
on dev, deploy-gate on dev, ``Runtime Profiles / validate``) could report red
on every PR forever with zero effect on mergeability.

:data:`EXPECTED_EXTERNAL_CONTEXTS` closes that gap: it is an ASSERTED tuple,
resolved against the PR head SHA's ``commits/{sha}/check-runs`` (a different
API surface than the in-run jobs list — this is what lets the poller see
check-runs produced by OTHER workflow files' runs), each required **present +
completed + conclusion == "success"** (:data:`EXTERNAL_GOOD_CONCLUSIONS` — a
*stricter* set than :data:`GOOD_CONCLUSIONS`: an externally skipped context
fails closed here, because none of the L4 entries carry a legitimate skip
precondition this gate re-derives — a skip is either a real failure-to-run or
a producer-side ``if:`` this gate does not re-verify, so it is not eligible
for the SKIPPABLE (L2) treatment). Missing or still-running is PENDING,
converted to FAILURE at the caller's deadline exactly like layers 1-3. An
unfetchable check-runs response (``check_runs is None``) is treated as
*every* L4 context unobserved — never a blind pass.

:data:`ACTOR_CONDITIONAL_CONTEXTS` names legitimate producer-side absences
in this set — a context whose producing workflow's own ``if:`` is false for a
named actor, so no check-run is ever created for that actor's PRs. It is an
applicability rule, not a bypass: scoped per context, per actor. The registry
is EMPTY as of OMN-16933; its only entry was the removed CodeRabbit thread
gate, whose caller carried ``if: github.actor != 'dependabot[bot]'``.

Jobs produced by other workflow files that are NOT in
:data:`EXPECTED_EXTERNAL_CONTEXTS` are exempt only by explicit, reasoned
classification — see ``tests/unit/scripts/ci/test_ci_summary_gate.py``'s
``EXEMPT_CONTEXTS`` and its completeness test
(``test_every_pr_triggered_job_is_classified``), which enumerates every job
reachable from ``on.pull_request`` across ``.github/workflows/*.yml`` and
proves STRICT | SKIPPABLE | EXTERNAL | EXEMPT covers it — a new, unclassified
workflow job fails that test until it is triaged into one of the four
buckets.

COVERAGE HONESTY — what ``CI Summary`` does not see even with L4
------------------------------------------------------------------
``EXPECTED_EXTERNAL_CONTEXTS`` is a fixed, curated tuple, not derived live
from branch protection. If a context is added to ``required_status_checks``
without a matching addition here (or removed from protection but left here),
the two drift apart silently — ``.github/required-checks.yaml`` plus its
``required-check-skip-guard`` gate is the durable cross-check for that drift;
this module does not read branch protection directly.

Exit codes: ``0`` success, ``1`` failure, ``2`` pending.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

# The poller's own job — excluded to avoid self-deadlock.
SELF_JOB_NAME = "CI Summary"

# The dynamic test-matrix jobs surface as "Tests (Split N/M)".
TEST_SPLIT_PREFIX = "Tests (Split "

# The selector job whose success proves the test matrix should exist.
DETECT_CHANGES_JOB = "Detect Changes"

# Strict aggregate gates: present + completed + conclusion == success.
#
# Derivation (re-derived for omnimarket — NOT copied from omnibase_core). Each
# job below carries NO legitimate ``if:`` skip path in omnimarket's ci.yml: it
# is gated only on ``needs: occ-preflight`` (fail-propagation, not a skip) and
# runs on every gating event (pull_request / merge_group / push). It therefore
# never legitimately reports ``skipped`` on a valid PR, so a skip = anomaly =
# fail-closed. Names are the ``name:`` display strings the Actions jobs API
# returns (verified against a live run 29799112037 on 2026-07-21).
#
# ONE deliberate exception (OMN-16217, 2026-08-18): ``Coverage Sweep Gate``
# now carries a job-level ``if:`` that legitimately skips it on a DRAFT
# dev-targeting PR (the org-wide draft-state CI admission gate fan-out of
# onex_change_control#6686 / OMN-15731 — see the job's own comment in
# ci.yml). This is intentional and does NOT weaken this module: the job stays
# in STRICT_GATE_JOBS (not SKIPPABLE_GATE_JOBS), so a ``skipped`` conclusion
# — draft-induced or otherwise — still fails ``CI Summary`` closed. The "no
# legitimate if:" derivation above is otherwise unchanged for every other row.
STRICT_GATE_JOBS: tuple[str, ...] = (
    # OCC preflight dependency — step short-circuits to exit 0 on non-PR events;
    # no ``if:``, so the job is always present + completed.
    "OCC Preflight Dependency",
    # zone-filter reusable — it IS the docs-only classifier, so it always runs
    # (it never skips itself); only ``needs: occ-preflight``.
    "zone-filter / Zone Filter (docs-only check)",
    "lint",  # lint — needs occ-preflight, no if:
    "Topic Naming Lint",  # topic-naming-lint — needs occ-preflight, no if:
    "Runtime Sweep",  # runtime-sweep — needs occ-preflight, no if:
    "Compliance Sweep",  # compliance-sweep — needs occ-preflight, no if:
    "Core-Only Install Gate",  # core-only-install — needs occ-preflight, no if:
    # contract-compliance — its ``if:`` is
    # ``occ-preflight.result == 'success' && (pull_request||merge_group||push)``;
    # the event clause is always true (those are the only triggers), so it never
    # legitimately skips on a valid PR.
    "Contract Compliance Check",
    "Contract Sweep Gate",  # contract-sweep-gate — needs occ-preflight, no if:
    "Dependency Health Gate",  # dep-health — needs occ-preflight, no if:
    "uv.lock Pin Reachability",  # uv-lock-pin-reachability — needs occ-preflight, no if:
    "Aislop Sweep (strict, PR diff)",  # aislop-sweep — needs occ-preflight, no if:
    "Coverage Sweep Gate",  # coverage-sweep-gate — needs occ-preflight; strict per OMN-14645; OMN-16217 draft-gated on dev only (see block comment above)
    "Workflow Module Resolution",  # workflow-module-refs — needs occ-preflight, no if:
    "no-noncanonical-lifecycle-classes",  # unconditional (OMN-14350), no needs/if:
    # OMN-16774: whole event chains driven from the REAL contract.yaml through
    # the REAL handler and the REAL dispatch seam on the in-memory bus
    # (tests/chains/). THIS LINE IS HALF THE MECHANISM. The default-deny sweep
    # already fails CI Summary when the job FAILS, but an unregistered job that
    # is `skipped` or absent yields SUCCESS — so without this entry, deleting or
    # skipping the job would silently retire the only per-PR proof that a chain
    # still terminalizes. Unconditional in ci.yml (no needs/if), so a skip is
    # anomalous and never a legitimate opt-out. Registered because OMN-16767
    # proved the failure mode is SILENT: a contract edit in THIS repo took the
    # delegation chain to zero behind fully green CI, with every request landing
    # in the quarantine sink.
    "Event Chain Gate",  # event-chain-gate
    "Event Registry Drift",  # event-registry-drift — needs occ-preflight, no if: (strict per OMN-14590)
    "Topic Drift Check",  # topic-drift-check — needs occ-preflight, no if: (strict per OMN-14590)
    "Topic Enum Drift Check",  # topic-enum-drift — needs occ-preflight, no if: (strict per OMN-14590)
    "contract-topic-graph",  # unconditional (OMN-14582/14640), no needs/if: — strict
    "Merge Reason-Code Gate",  # merge-reason-code-gate — no needs/if: (strict per OMN-14765)
    # OMN-15427 (port of the OMN-15214 canary / OMN-15221 omniclaude port):
    # every OCC evidence citation in the PR body must be MERGED/durable before
    # this product PR may merge. Unconditional in ci.yml (no needs/if:), so a
    # skipped/cancelled conclusion is anomalous and fails closed here — the
    # strict slot IS the enforcement (detection alone is rule-5 noncompliance).
    # omnimarket#1953 cited a CLOSED-unmerged companion (OCC#5487) and no
    # omnimarket CI surface caught it; this row is what makes that RED.
    "OCC Companion Merged Gate (OMN-15214)",
    # OMN-15483: the consumer-independent merge-hold enforcement point. The
    # merge NODE honoring the hold marker binds one consumer; the foreground
    # Codex controller that performed every merge in OMN-15483's incident
    # table contains no omnimarket code and stays unbound by any amount of it.
    # Registering the hold gate HERE is what binds every consumer at once: a
    # held PR fails this job, this job is strict, "CI Summary" is required, so
    # a held PR can never be required-green for the Codex controller,
    # `gh pr merge`, auto-merge, or the node path alike. Unconditional in
    # ci.yml (no needs/if:), so skipped/cancelled is anomalous and fails closed
    # here — detection without enforcement is rule-5 noncompliance, and the
    # strict slot IS the enforcement.
    "Merge Hold Gate (OMN-15483)",
    # OMN-16344: release-identity gate — blocks merging a packaged-source change
    # onto an already-published version string, the mechanism that keeps dev's
    # project.version ahead of the last released tag (omnibase_infra OMN-13412,
    # omnibase_core OMN-13411). Unconditional in ci.yml (no needs/if:), so a
    # skipped/cancelled conclusion is anomalous and fails closed here.
    #
    # The strict slot IS the enforcement, and it is why this gate needs no
    # branch-protection change: "CI Summary" is already a live required context
    # on both dev and main, so a PR that aliases two code states under one
    # version can never be required-green. Detection alone would be rule-5
    # noncompliance — omnimarket's dev sat at 0.4.8, identical to the published
    # v0.4.8 tag, through seven commits of src/ changes precisely because no
    # surface made it RED.
    "Release Identity Gate",
)

# Skippable aggregate gates: present + completed + success OR skipped.
#
# Derivation: EXACTLY the jobs gated behind
# ``needs.zone-filter.outputs.docs_only != 'true'`` in omnimarket's ci.yml
# (directly, or — for ``test`` handled by the matrix rule — via a ``needs`` on
# ``detect-changes`` which is itself docs-only-gated). A docs-only PR
# legitimately skips these, so ``skipped`` is accepted. This is the point the
# plan makes explicit: a job strict in omnibase_core may legitimately skip in
# omnimarket — here the entire E2E/golden/typecheck lane is docs-only-gated.
SKIPPABLE_GATE_JOBS: tuple[str, ...] = (
    "typecheck",  # if: docs_only != 'true'
    "Detect Changes",  # if: docs_only != 'true' (also drives the matrix rule)
    "Integration Silent-Skip Guard (OMN-14172)",  # if: docs_only != 'true'
    "E2E Workflow Runner (inmemory bus)",  # if: docs_only != 'true'
    "Golden Chain Suite (inmemory bus)",  # if: docs_only != 'true'
    "SEA E2E Acceptance + Error Chains (OMN-12660)",  # if: docs_only != 'true'
    "Generated-Node Golden Chain Gate (OMN-13624)",  # if: docs_only != 'true'
)

# --------------------------------------------------------------------------- #
# OMN-16662: the docs-only skip tier.
# --------------------------------------------------------------------------- #
#
# Operator ruling: a Markdown / badge / README PR must not pay for the heavy
# code-verification suite, while the doc gates keep running. Measured on the
# docs-only PR #2148 (head 67d49549): ``Coverage Sweep Gate`` alone burned 22 of
# ci.yml's 55 runner-minutes. ci.yml's own header already calls it "the
# longest-pole job (3 fresh sibling clones + ``uv sync --all-extras`` + full
# ``pytest --cov``)". A Markdown diff cannot move a coverage census.
#
# WHY THIS IS NOT JUST A MOVE INTO ``SKIPPABLE_GATE_JOBS``
# -------------------------------------------------------
# ``Coverage Sweep Gate``'s STRICT membership is load-bearing for OMN-16217:
# that draft-state admission gate depends on "a ``skipped`` conclusion --
# draft-induced or otherwise -- still fails CI Summary closed", so a draft PR's
# gate reads RED rather than green-by-skip. An unconditional move to
# ``SKIPPABLE_GATE_JOBS`` would silently delete that property. The relaxation
# therefore has to be CONDITIONAL on the diff actually being docs-only, and the
# name stays in ``STRICT_GATE_JOBS`` (asserted by
# ``test_docs_only_tier_is_subset_of_strict_gates``).
#
# WHY A MARKER JOB AND NOT ``needs.zone-filter.outputs.docs_only``
# ---------------------------------------------------------------
# omnibase_core's OMN-16625 pilot could gate its ``quality-gate`` aggregator by
# adding ``needs: [zone-filter]`` and reading the output directly. That is not
# available here: ``ci-summary`` is a NO-``needs`` poller *on purpose*
# (OMN-14127 -- a ``needs``-gated job gets no check-run until its needs
# terminalize, which is exactly how the old gate went ABSENT under fleet
# saturation and wedged PRs BLOCKED with 0 failing / 0 pending), and job
# OUTPUTS do not appear in the ``actions/runs/{id}/jobs`` payload this module
# reads. So the bit is carried by a job the poller can already see:
# ``docs-only-marker``, whose own ``if:`` is
# ``always() && needs.zone-filter.outputs.docs_only == 'true'``.
#
# FAIL-CLOSED BY CONSTRUCTION, not by an added guard: the ONLY state that
# relaxes anything is a marker that actually RAN and concluded ``success``.
# Absent, in_progress, ``skipped``, ``cancelled``, ``failure`` -- every one of
# them means "not docs-only", so the default on every ordinary code PR (where
# the marker's ``if:`` is false) is full strictness. The marker is not a
# caller-supplied flag and cannot be set by hand; its authority is the reusable
# zone-filter classifier, which requires EVERY changed path to classify
# ``EnumFileZone.DOCS``.
#
# The relaxation is PER-NAME, never a blanket ``|| skipped`` -- the same policy
# ``tests-gate`` already applies per-upstream (OMN-15315). Every gate outside
# this tier must still be exactly ``success`` on a docs-only diff, which is what
# keeps the contract/doc/evidence gates (``Contract Compliance Check``, the
# sweeps, ``Leaked Literals Gate``, the OCC gates) running -- the half of the
# operator ruling that is not about saving minutes.
DOCS_ONLY_MARKER_JOB = "Docs-Only Marker (OMN-16662)"

DOCS_ONLY_SKIPPABLE_GATE_JOBS: tuple[str, ...] = ("Coverage Sweep Gate",)

# Every job the completeness anchor must observe present+good for SUCCESS.
GATE_JOBS: tuple[str, ...] = STRICT_GATE_JOBS + SKIPPABLE_GATE_JOBS

# Jobs that do NOT gate merge today (verified against ci.yml on 2026-07-21). The
# default-deny sweep ignores these so it never newly-wedges a PR on a job that is
# already non-blocking. Keep this list SMALL and only add jobs that genuinely
# already exist in ci.yml as non-gating.
SOFT_ALLOWLIST: frozenset[str] = frozenset(
    {
        # coverage-aggregate-shadow: shadow/advisory rollup, not in the old
        # ci-summary ``needs`` and not a required branch-protection context.
        "Coverage Aggregate (shadow)",
    }
)

# L4: contexts produced by OTHER workflow files (separate runs the in-run
# poller never observes), ASSERTED against the PR head SHA's
# `commits/{sha}/check-runs`. Superset of the old UNSUMMARIZED_REQUIRED_CONTEXTS
# doc-only list: every entry that was already live-required on dev and/or main
# stays here (now enforced, not merely disclosed); `reason-graph` was dropped
# (removed from branch protection 2026-07-25 — product-readiness-shadow.yml is
# a self-declared non-required Phase-3 canary, see EXEMPT_CONTEXTS in the test
# file); `shell-hygiene` was dropped for the same reason (self-declared
# staged/deferred promotion, see EXEMPT_CONTEXTS); `verify / verify` and
# `call-reject-skip-token / scan / reject-skip-gate-token` were added (live
# main-required, missing from the old disclosure list). NEW gap closures
# (never required anywhere before this gate): `fsm-handler-drift`,
# `skill-mapping-input-coverage-gate`, `node-migration-vendor-parity-gate`,
# `node-drift-gate` (was main-only), `contract-validation` (was main-only),
# `deploy-gate / deploy-gate` (was main-only), `validate` (from
# validator-runtime-profiles.yml — its own header claimed the required-
# status-check name is "Runtime Profiles / validate", which is FALSE: that
# job has no job-level `uses:`, so GitHub names its check-run after the job's
# own `name:` field only, exactly like the sibling bare-name `state-coverage-
# gate` job — "validate", not workflow-name-prefixed. Corrected in the same
# PR that adds this assertion.).
#
# `receipt-honesty` (OMN-16878 AC3): promoted out of EXEMPT_CONTEXTS, where it
# sat as "self-declared staged, not yet promoted" — receipt-honesty.yml's own
# header said only "Required-status-check name (when later flipped)", a
# deferral with no unmet technical precondition, not a genuine blocker.
# `contract-validation` was already asserted here; the sibling gap this closes
# is that omniclaude and omnibase_infra both already carry `receipt-honesty`
# in their own equivalent tuples (OMN-16878 AC1/AC2) while omnimarket's own
# CI ran the same producer on every PR and could not block on it — Operating
# Rule 5 (detection not wired as a pre-merge gate is advisory and gets
# ignored). Admission: 10/10 recent merged dev PR heads sampled
# (#2194-#2203, 2026-08-29) present and green, consistent with the ticket's
# original 16/16 measurement; non-vacuity is the shared
# `omnibase_core.validation.validator_receipt_honesty` proof already recorded
# for the sibling repos (OCC#7433 dod-nonvacuity-negative-tests: gamed
# receipt exits 1, real committed receipt exits 0). Pinned by
# `tests/unit/scripts/ci/test_omn_16878_omnimarket_receipt_honesty.py`.
EXPECTED_EXTERNAL_CONTEXTS: tuple[str, ...] = (
    "Architectural Compliance Lint",
    "Canonical Inference Gate",
    "CI Naming Convention",
    "Dep Provenance Gate",
    "Ecosystem Integration Validation",
    "Enforce validator-requirements.yaml (OMN-13291)",
    "Hostile Review Gate",
    "Hostile Reviewer (adversarial gate)",
    "Leaked Literals Gate",
    "Legacy Compatibility Check",
    "No Faked Boundary Gate",
    "OCC Emitter Golden Gate",
    "Omni Standards Gate",
    "ONEX Change Control Schema Compatibility",
    "PR Arch Review Gate",
    "Precommit Fail-Loud Gate",
    "Projection Exposure Drift Gate",
    "Repository Structure Validation",
    "Resolve Bot Token",
    "Stale TODO Gate",
    "URL Authority Gate",
    "call / validate-docs",
    "call-reject-skip-token / occ-preflight / eligibility",
    "call-reject-skip-token / scan / reject-skip-gate-token",
    "contract-validation",
    "deploy-gate / deploy-gate",
    "dispatcher-route-coverage",
    "fsm-handler-drift",
    "imperative-contract-guard / Imperative Contract Guard",
    "main-target-guard",
    "node-drift-gate",
    "node-migration-vendor-parity-gate",
    "non-dev-base-guard",
    "occ-preflight / eligibility",
    "pr-title / check-title",
    "receipt-honesty",
    "required-check-skip-guard / check-skip-vectors",
    "skill-mapping-input-coverage-gate",
    "state-coverage-gate",
    "subscriber-dispatcher-resolution",
    "validate",
    "verify / verify",
)

# Conclusions that count as "provably passed" for an L4 external context. A
# STRICTER set than GOOD_CONCLUSIONS: no L4 entry carries a skip precondition
# this gate re-derives, so "skipped" is not eligible for L2-style leniency here
# — it fails closed.
EXTERNAL_GOOD_CONCLUSIONS: frozenset[str] = frozenset({"success"})

# Contexts that are legitimately ABSENT (no check-run at all) for one specific
# actor, because the producer's own `if:` skips the job for that actor before
# any check-run is created. An applicability rule, not a bypass — scoped per
# context, per actor.
# EMPTY BY CONSTRUCTION as of OMN-16933, not by omission: the only entry was
# `gate / CodeRabbit Thread Check`, whose caller carried
# `if: github.actor != 'dependabot[bot]'`. CodeRabbit was removed entirely
# (operator ruling 2026-08-29) and cr-thread-gate*.yml are deleted, so no live
# producer is actor-scoped. The MECHANISM stays — `evaluate_external` still
# takes `actor_conditional`, ci.yml still passes `--actor`, and the tests
# still exercise it through an injected synthetic registry — because the next
# actor-scoped producer needs it and re-deriving it costs a wedged lane.
ACTOR_CONDITIONAL_CONTEXTS: dict[str, frozenset[str]] = {}

# Conclusions that count as "provably passed".
GOOD_CONCLUSIONS: frozenset[str] = frozenset({"success", "skipped"})

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_PENDING = 2


@dataclass(frozen=True)
class JobState:
    """The latest-attempt state of a single workflow job."""

    name: str
    status: str  # queued | in_progress | completed | waiting | ...
    conclusion: str | None  # success | failure | cancelled | skipped | timed_out | None
    run_attempt: int


def _state_severity(job: JobState) -> int:
    """Rank same-attempt duplicate jobs by the most blocking state."""

    if job.status != "completed":
        return 2
    if job.conclusion not in GOOD_CONCLUSIONS:
        return 3
    return 1


def dedup_latest(
    jobs: list[dict[str, object]],
    *,
    run_attempt: int | None = None,
) -> dict[str, JobState]:
    """Collapse the raw ``/runs/{id}/jobs`` array to one entry per job name.

    When ``run_attempt`` is provided, only rows from that workflow attempt are
    considered. This prevents stale failed/cancelled rows from an earlier
    attempt from becoming authoritative for a current rerun. Within the same
    attempt, duplicate display names keep the most blocking state so a failed
    matrix leg cannot be hidden by a later same-name success.
    """

    latest: dict[str, JobState] = {}
    for raw in jobs:
        name = str(raw.get("name") or "")
        if not name:
            continue
        try:
            attempt = int(str(raw.get("run_attempt") or 1))
        except (TypeError, ValueError):
            attempt = 1
        if run_attempt is not None and attempt != run_attempt:
            continue
        prev = latest.get(name)
        if prev is not None and attempt < prev.run_attempt:
            continue
        conclusion = raw.get("conclusion")
        current = JobState(
            name=name,
            status=str(raw.get("status") or ""),
            conclusion=None if conclusion is None else str(conclusion),
            run_attempt=attempt,
        )
        if (
            prev is not None
            and attempt == prev.run_attempt
            and _state_severity(current) < _state_severity(prev)
        ):
            continue
        latest[name] = current
    return latest


def _is_allowlisted(name: str, allowlist: frozenset[str]) -> bool:
    """Prefix-aware allowlist check.

    A reusable-workflow caller's inner jobs surface in the jobs API as
    ``"<caller display name> / <inner job name>"``; matching the caller segment
    lets a single allowlist entry cover all of its inner jobs.
    """

    if name in allowlist:
        return True
    caller = name.split(" / ", 1)[0]
    return caller in allowlist


def _evaluate_test_matrix(
    latest: dict[str, JobState],
    *,
    detect_changes_job: str = DETECT_CHANGES_JOB,
    split_prefix: str = TEST_SPLIT_PREFIX,
) -> str:
    """Return one of ``"ok"``, ``"pending"``, ``"waived"`` for the test matrix.

    * ``waived``  — ``Detect Changes`` is not present+completed+``success`` (a
      docs-only skip, or a failure already caught by the skippable-gate check).
      The dynamic matrix is not required.
    * ``pending`` — ``Detect Changes`` succeeded but the splits are not all
      terminal yet (or none created yet). Keep polling.
    * ``ok``      — ``Detect Changes`` succeeded and every present split job is
      completed (at least one exists). A *failed* split is NOT flagged here —
      it is caught by the default-deny sweep — so ``ok`` means "the matrix ran
      to completion", not "the matrix passed".
    """

    dc = latest.get(detect_changes_job)
    if dc is None or dc.status != "completed" or dc.conclusion != "success":
        return "waived"

    splits = [s for name, s in latest.items() if name.startswith(split_prefix)]
    if not splits:
        # detect_test_paths always emits split_count >= 1 when it runs, so zero
        # splits present after a successful detect-changes is always the
        # asynchronous creation window — never a legitimate empty matrix.
        return "pending"
    if any(s.status != "completed" for s in splits):
        return "pending"
    return "ok"


def evaluate(
    jobs: list[dict[str, object]],
    *,
    run_attempt: int | None = None,
    self_name: str = SELF_JOB_NAME,
    strict_gates: tuple[str, ...] = STRICT_GATE_JOBS,
    skippable_gates: tuple[str, ...] = SKIPPABLE_GATE_JOBS,
    allowlist: frozenset[str] = SOFT_ALLOWLIST,
    docs_only_marker: str = DOCS_ONLY_MARKER_JOB,
    docs_only_gates: tuple[str, ...] = DOCS_ONLY_SKIPPABLE_GATE_JOBS,
) -> tuple[int, str]:
    """Return ``(exit_code, human_report)`` for the current job snapshot."""

    latest = dedup_latest(jobs, run_attempt=run_attempt)
    gate_names = frozenset(strict_gates) | frozenset(skippable_gates)

    # OMN-16662: derive docs_only from the in-run marker job, never from a
    # caller-supplied argument. ONLY a marker that ran and concluded success
    # relaxes anything — absent / in_progress / skipped / cancelled / failure
    # all leave every gate strict. See the DOCS_ONLY_MARKER_JOB block above for
    # why the bit travels as a job rather than as `needs.<job>.outputs`.
    marker_state = latest.get(docs_only_marker)
    docs_only = (
        marker_state is not None
        and marker_state.status == "completed"
        and marker_state.conclusion == "success"
    )
    # Relaxed ⊆ strict_gates: a name that is not strict cannot be "relaxed" into
    # existence, so a tier entry dropped from STRICT_GATE_JOBS degrades to a
    # no-op here instead of silently becoming permanently skippable.
    relaxed = (
        frozenset(docs_only_gates) & frozenset(strict_gates)
        if docs_only
        else frozenset()
    )

    # (1) Strict aggregate gates: present + completed + conclusion == success.
    #     Members of `relaxed` widen to success/skipped for THIS run only; a
    #     `failure`/`cancelled` conclusion is never admitted, docs-only or not.
    strict_failures = sorted(
        g
        for g in strict_gates
        if (
            (st := latest.get(g)) is not None
            and st.status == "completed"
            and (
                st.conclusion not in GOOD_CONCLUSIONS
                if g in relaxed
                else st.conclusion != "success"
            )
        )
    )

    # (2) Skippable aggregate gates: present + completed + success/skipped.
    skippable_failures = sorted(
        g
        for g in skippable_gates
        if (
            (st := latest.get(g)) is not None
            and st.status == "completed"
            and st.conclusion not in GOOD_CONCLUSIONS
        )
    )

    # (4) Default-deny sweep over every OTHER present+completed job. Failed
    #     "Tests (Split N/M)" splits are caught here (they are not gate_names
    #     and not allowlisted).
    sweep_failures = sorted(
        j.name
        for name, j in latest.items()
        if name != self_name
        and name not in gate_names
        and not _is_allowlisted(name, allowlist)
        and j.status == "completed"
        and j.conclusion not in GOOD_CONCLUSIONS
    )

    # (3) Test-matrix completeness (dynamic split jobs).
    matrix_state = _evaluate_test_matrix(latest)

    # Completeness anchor: every named gate present AND completed.
    gate_missing_or_pending = [
        g
        for g in (*strict_gates, *skippable_gates)
        if (latest.get(g) is None or latest[g].status != "completed")
    ]

    all_failures = strict_failures + skippable_failures + sweep_failures

    def _rep(verdict: str) -> str:
        return _report(
            verdict,
            latest,
            strict_gates,
            skippable_gates,
            strict_failures,
            skippable_failures,
            sweep_failures,
            gate_missing_or_pending,
            matrix_state,
            docs_only=docs_only,
            relaxed=relaxed,
        )

    # Failures win over pending: a proven bad job is terminal, no need to wait.
    if all_failures:
        return EXIT_FAILURE, _rep("FAILURE")
    if gate_missing_or_pending or matrix_state == "pending":
        return EXIT_PENDING, _rep("PENDING")
    return EXIT_SUCCESS, _rep("SUCCESS")


def _report(
    verdict: str,
    latest: dict[str, JobState],
    strict_gates: tuple[str, ...],
    skippable_gates: tuple[str, ...],
    strict_failures: list[str],
    skippable_failures: list[str],
    sweep_failures: list[str],
    gate_missing_or_pending: list[str],
    matrix_state: str,
    *,
    docs_only: bool = False,
    relaxed: frozenset[str] = frozenset(),
) -> str:
    lines = [f"CI Summary verdict: {verdict}", f"  jobs observed: {len(latest)}"]
    # OMN-16662: make the relaxation visible in the job summary. A reviewer must
    # be able to read off WHY a strict gate was allowed to skip, and see the
    # marker state that authorised it — silent relaxation is how a skip tier
    # turns into an unnoticed bypass.
    marker_state = latest.get(DOCS_ONLY_MARKER_JOB)
    lines.append(
        "  docs-only marker: "
        + (
            "<absent>"
            if marker_state is None
            else f"{marker_state.status}/{marker_state.conclusion}"
        )
        + f" -> docs_only={str(docs_only).lower()}"
    )
    if relaxed:
        lines.append(
            "  docs-only skip tier ACTIVE (success|skipped accepted for): "
            + ", ".join(sorted(relaxed))
        )
    lines.append("  strict gates (must be completed + success):")
    for g in strict_gates:
        st = latest.get(g)
        marker = "  [docs-only tier]" if g in relaxed else ""
        lines.append(
            f"    - {g}: <absent>{marker}"
            if st is None
            else f"    - {g}: {st.status}/{st.conclusion}{marker}"
        )
    lines.append("  skippable gates (completed + success/skipped):")
    for g in skippable_gates:
        st = latest.get(g)
        lines.append(
            f"    - {g}: <absent>"
            if st is None
            else f"    - {g}: {st.status}/{st.conclusion}"
        )
    lines.append(f"  test matrix: {matrix_state}")
    if strict_failures:
        lines.append(f"  strict-gate failures: {', '.join(strict_failures)}")
    if skippable_failures:
        lines.append(f"  skippable-gate failures: {', '.join(skippable_failures)}")
    if sweep_failures:
        lines.append(f"  default-deny sweep failures: {', '.join(sweep_failures)}")
    if gate_missing_or_pending:
        lines.append(f"  gates missing/pending: {', '.join(gate_missing_or_pending)}")
    lines.append(
        "  NOTE: the in-run layers above (1-3) summarize ci.yml jobs ONLY; "
        f"{len(EXPECTED_EXTERNAL_CONTEXTS)} contexts from OTHER workflow files "
        "are asserted separately as L4 EXPECTED_EXTERNAL_CONTEXTS — see the "
        "external-context section of this report if present."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Layer 4: external contexts (other workflow files), via commits/{sha}/check-runs.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckRunState:
    """The latest-attempt state of a single GitHub check-run (by display name)."""

    name: str
    status: str  # queued | in_progress | completed | ...
    conclusion: str | None
    started_at: str  # ISO-8601; used only to break ties between duplicate rows.


def _check_run_severity(state: CheckRunState) -> int:
    """Rank a check-run row by how blocking it is: bad > incomplete > good.

    A more-recent-but-less-blocking row (e.g. a fresh success superseding a
    stale in-flight rerun row) still wins by ``started_at`` — see
    :func:`dedup_latest_check_runs`. This ranking only breaks ties when two
    rows are otherwise indistinguishable in time, and guarantees a completed
    non-good conclusion is never silently dropped in favor of an incomplete
    duplicate row for the same check name.
    """

    if state.status != "completed":
        return 1
    if state.conclusion not in EXTERNAL_GOOD_CONCLUSIONS:
        return 2
    return 0


def dedup_latest_check_runs(
    check_runs: list[dict[str, object]],
) -> dict[str, CheckRunState]:
    """Collapse a raw ``commits/{sha}/check-runs`` array to one row per name.

    The GitHub API call this feeds is expected to pass ``filter=latest``
    (one row per check name already), so this is defense-in-depth: rows are
    kept by most-recent ``started_at``; a tie in ``started_at`` is broken by
    the more-blocking row (:func:`_check_run_severity`) so a stale duplicate
    can never hide a real failure.
    """

    latest: dict[str, CheckRunState] = {}
    for raw in check_runs:
        name = str(raw.get("name") or "")
        if not name:
            continue
        conclusion = raw.get("conclusion")
        current = CheckRunState(
            name=name,
            status=str(raw.get("status") or ""),
            conclusion=None if conclusion is None else str(conclusion),
            started_at=str(raw.get("started_at") or ""),
        )
        prev = latest.get(name)
        if prev is None:
            latest[name] = current
            continue
        if current.started_at > prev.started_at or (
            current.started_at == prev.started_at
            and _check_run_severity(current) > _check_run_severity(prev)
        ):
            latest[name] = current
    return latest


def evaluate_external(
    check_runs: list[dict[str, object]] | None,
    *,
    actor: str | None = None,
    expected: tuple[str, ...] = EXPECTED_EXTERNAL_CONTEXTS,
    actor_conditional: dict[str, frozenset[str]] = ACTOR_CONDITIONAL_CONTEXTS,
) -> tuple[int, str]:
    """Return ``(exit_code, human_report)`` for the L4 external-context layer.

    ``check_runs is None`` means the ``commits/{sha}/check-runs`` fetch itself
    failed — every expected context is treated as unobserved (PENDING), never
    as a blind pass.
    """

    if check_runs is None:
        lines = [
            "External contexts verdict: PENDING",
            "  check-runs fetch failed or not yet attempted; "
            f"{len(expected)} expected external contexts unobserved.",
        ]
        return EXIT_PENDING, "\n".join(lines)

    latest = dedup_latest_check_runs(check_runs)
    failures: list[str] = []
    missing: list[str] = []

    for name in expected:
        st = latest.get(name)
        if st is None:
            allowed_actors = actor_conditional.get(name)
            if (
                allowed_actors is not None
                and actor is not None
                and actor in allowed_actors
            ):
                continue  # legitimately absent for this actor — not a gap.
            missing.append(name)
            continue
        if st.status != "completed":
            missing.append(name)
            continue
        if st.conclusion not in EXTERNAL_GOOD_CONCLUSIONS:
            failures.append(name)

    lines = [f"External contexts asserted: {len(expected)}"]
    if failures:
        lines.append(f"  external-context failures: {', '.join(sorted(failures))}")
    if missing:
        lines.append(
            f"  external-context missing/pending: {', '.join(sorted(missing))}"
        )

    if failures:
        return EXIT_FAILURE, "\n".join(lines)
    if missing:
        return EXIT_PENDING, "\n".join(lines)
    lines.append("  all external contexts present + success.")
    return EXIT_SUCCESS, "\n".join(lines)


def _worse(a: int, b: int) -> int:
    """FAILURE beats PENDING beats SUCCESS, regardless of numeric exit value."""

    order = {EXIT_FAILURE: 2, EXIT_PENDING: 1, EXIT_SUCCESS: 0}
    return a if order[a] >= order[b] else b


def _load_jobs(path: str | None) -> list[dict[str, object]]:
    if path is None or path == "-":
        raw = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    data = json.loads(raw)
    # Accept either the raw endpoint object ({"jobs": [...]}) or a bare array.
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise ValueError("jobs payload must be a list or an object with a 'jobs' array")
    return jobs


def _load_check_runs(path: str | None) -> list[dict[str, object]] | None:
    """Load a ``commits/{sha}/check-runs`` payload. ``None`` means "not fetched"."""

    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return None
    if not raw.strip():
        return None
    data = json.loads(raw)
    check_runs = data.get("check_runs", []) if isinstance(data, dict) else data
    if not isinstance(check_runs, list):
        raise ValueError(
            "check-runs payload must be a list or an object with a 'check_runs' array"
        )
    return check_runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jobs-file",
        default="-",
        help="Path to the GitHub Actions jobs JSON (default: stdin). Accepts the "
        "raw endpoint object or a bare array of job objects.",
    )
    parser.add_argument(
        "--check-runs-file",
        default=None,
        help="Path to the commits/{sha}/check-runs JSON for the PR head SHA "
        "(L4 external-context layer). Omit to skip L4 evaluation entirely "
        "(exit code reflects layers 1-3 only) — used by the unit tests and by "
        "any caller not yet wired for L4.",
    )
    parser.add_argument(
        "--actor",
        default=None,
        help="github.actor for the current run, used to resolve "
        "ACTOR_CONDITIONAL_CONTEXTS (a legitimately-absent context for one "
        "specific actor).",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Print the verdict report and exit 0 regardless (diagnostics only).",
    )
    parser.add_argument(
        "--run-attempt",
        type=int,
        default=None,
        help="Evaluate only rows for this GitHub Actions run_attempt.",
    )
    args = parser.parse_args(argv)

    jobs = _load_jobs(args.jobs_file)
    code, report = evaluate(jobs, run_attempt=args.run_attempt)
    print(report)

    if args.check_runs_file is not None:
        check_runs = _load_check_runs(args.check_runs_file)
        ext_code, ext_report = evaluate_external(check_runs, actor=args.actor)
        print(ext_report)
        code = _worse(code, ext_code)

    if args.report_only:
        return EXIT_SUCCESS
    return code


if __name__ == "__main__":
    raise SystemExit(main())
