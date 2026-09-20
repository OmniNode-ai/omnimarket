# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge-check reason-code classifier (OMN-14765, epic OMN-14643, WS merge-flow).

Root cause this module addresses
--------------------------------
The merge controller (``merge_sweep`` skill -> ``node_pr_lifecycle_orchestrator``)
decides whether a failed CI check is a *product* failure (dispatch a code fix), a
*rerunnable* failure (cheap ``gh run rerun``), or a *stale/withhold* signal by
GUESSING from the check NAME and the ``gh pr checks`` bucket. That surface is
unreliable in exactly the ways the 2026-07-16 overnight sweep documented:

* ``gh pr checks`` renders ``cancelled`` as ``fail`` (F-10) -> naive red-counting
  either dispatches a wrong code fix or storms reruns;
* GitHub API HTML/503/timeout responses poison decision loops (F-07);
* self-hosted runner checkout/setup failures look like product reds (F-08/F-25);
* stale/superseded ``Actions`` event payloads survive PR body/branch fixes
  (F-09/F-14/F-26).

This module is the deterministic, network-free classifier that keys on the
*jobs-API attempt* facts (``runs/<id>/jobs`` — the failed step, the run event,
the run head SHA, the run attempt, the required-context flag, and any job-log
infra/outage signatures) and returns exactly one typed
:class:`EnumMergeCheckReasonCode`. Data collection (the ``gh api`` call) lives in
the inventory node; this module only classifies.

Design invariants (deliberately mirror ``scripts/ci/product_readiness.py``)
---------------------------------------------------------------------------
- **No network I/O, stdlib only.** Callers resolve the jobs-API payload and pass
  facts in. Keeping the module import-light lets the fixture-corpus gate
  (``scripts/ci/check_merge_reason_codes.py``) run it under a bare
  ``setup-python`` step with no ``uv sync``.
- **Fail closed.** An unrecognized or indeterminate ``failure`` — a failed check
  with no affirmatively-identified *product* step — maps to ``RUNNER_INFRA``,
  never ``PRODUCT_FAILED``. Falsely calling infra "product" is the expensive
  documented failure (wrong code-fix dispatch / wrong merge block); a false
  ``RUNNER_INFRA`` costs only a bounded rerun the controller already caps.
- **Deterministic precedence.** When several signals are present the
  highest-precedence one wins, so a single source revision (not each poller)
  decides the diagnosis. As amended by OMN-18902::

      SAME_SHA_RERUN_RESCUE > STALE_CONTEXT > GITHUB_API_OUTAGE > RUNNER_INFRA
      > PROCESS_GATE_REFUSED > CANCELLED > PRODUCT_FAILED

OMN-18902: the two amendments, and why each is where it is
-----------------------------------------------------------
Measured 2026-09-20 over the 141 failing steps of a 13-ticket sample (every
number here was re-derived from the live jobs API before this change landed,
and the derivation is pinned by ``tests/merge_control/fixtures/
omn18902_measured_failing_steps_2026_09_20.json``):

* **``PROCESS_GATE_REFUSED`` is a third cause, and it is the DOMINANT one.**
  118 of those 141 steps -- 83.7 percent -- are a governance gate refusing
  because a lane did not produce a required evidence artifact. That is neither
  a product failure nor a broken machine, so under the five-member vocabulary
  every one of them fell through the fail-closed default and was recorded as
  ``RUNNER_INFRA``. The default is **not** loosened here: it still answers
  ``RUNNER_INFRA`` for a genuinely unrecognised step, and the new member is
  reached only by an affirmative match against
  ``_PROCESS_GATE_STEP_SUBSTRINGS``.

  It is ranked ABOVE ``PRODUCT_FAILED`` deliberately. Two measured gate steps,
  "Check for integration tests in PR" and "Refuse when the test shards did not
  run", carry the substring ``test``; with product ranked first the coverage
  gate among them would dispatch a code fix at a missing-artifact refusal,
  which is the exact wrong-diagnosis class this module exists to remove.

* **The same-SHA re-run rescue outranks every step-name match.** Across the
  same 1,635 runs, 154 went green only on a later attempt of an UNCHANGED head
  SHA. A step whose own name says ``pytest`` but whose commit reached green on
  a bare re-run did not fail on the product, whatever it is called, so the
  rescue fact is consulted first and answers ``RUNNER_INFRA``. The fact is
  supplied by the caller and defaults to ``False``, so every pre-existing
  caller's verdict is byte-identical to what it was before this change.

* **Verdict provenance is now reported.** ``classify_verdict`` returns whether
  the code was reached AFFIRMATIVELY or by failing closed. The two are
  indistinguishable in the bare code, which is why no consumer could report an
  honest unrecognised share: the fail-closed bucket and the affirmatively
  identified infra bucket are the same enum member. ``classify`` is unchanged
  in signature and return type and remains the entrypoint for every consumer
  that only wants the code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EnumMergeCheckReasonCode(StrEnum):
    """Why a single required CI check is not green (exactly one per check).

    - ``STALE_CONTEXT``: the check belongs to a superseded attempt / an old head
      SHA / a non-PR-associated run event on a required context. A fix already
      landed; the controller should refresh/supersede, not fix (F-09/F-14/F-26).
    - ``GITHUB_API_OUTAGE``: the GitHub API itself returned HTML/503/timeout, or
      the job log carries an API-outage signature. Withhold REST-dependent
      mutation until recovery (F-07).
    - ``RUNNER_INFRA``: a runner/environment setup step failed (checkout,
      setup-uv, setup-python, "Set up job"), the log matches a network/clone
      infra signature, or the run hit an isolation/hard-timeout hang (F-08/F-23/
      F-25). Cheap ``gh run rerun`` clears it.
    - ``CANCELLED``: the job conclusion is ``cancelled``/``timed_out`` with no
      identified product-step failure — a clean cancellation, never a product
      red (F-10). Rerun.
    - ``PROCESS_GATE_REFUSED``: a GOVERNANCE gate refused because a required
      evidence artifact is missing or not yet durable -- an evidence-source
      resolution, a change-control preflight or eligibility check, a receipt
      or companion gate, a version-bump gate, a review-verdict gate, a base
      branch check, a coverage gate (OMN-18902). Neither a product failure nor
      a broken machine: the remedy is to produce the artifact, so the
      controller must not dispatch a code fix and a rerun will not help.
    - ``PRODUCT_FAILED``: a product step (lint/type/test/coverage/build) failed
      with a real ``failure`` conclusion. Dispatch a code fix.
    """

    STALE_CONTEXT = "stale_context"
    GITHUB_API_OUTAGE = "github_api_outage"
    RUNNER_INFRA = "runner_infra"
    PROCESS_GATE_REFUSED = "process_gate_refused"
    CANCELLED = "cancelled"
    PRODUCT_FAILED = "product_failed"


# GitHub treats these run events as PR-associated; branch protection credits only
# their conclusions toward a required context. A required context reporting from
# any other event (e.g. a manually dispatched ``workflow_dispatch`` "CI Summary")
# is stale w.r.t. the PR (mirrors inventory ``_PR_ASSOCIATED_EVENTS``, OMN-13319).
_PR_ASSOCIATED_EVENTS: frozenset[str] = frozenset(
    {"pull_request", "pull_request_target"}
)

# Job-STEP name substrings that identify a runner/environment setup failure (the
# failure is in provisioning, not in product logic). Checkout / uv / python /
# sibling-repo clone / action-tarball download / job teardown.
_RUNNER_INFRA_STEP_SUBSTRINGS: tuple[str, ...] = (
    "set up job",
    "set up runner",
    "checkout",
    "setup-uv",
    "set up uv",
    "setup uv",
    "install uv",
    "setup-python",
    "set up python",
    "setup python",
    "clone omni",  # "Clone omnibase_core" sibling-repo steps
    "download action",
    "post ",  # GitHub's auto-generated "Post <action>" teardown steps
    "complete job",
    "initialize containers",
    "start containers",
    # OMN-18902: the CI Summary poller. 11 of the 141 measured failing steps
    # are this one. It is the fail-closed verdict job giving up on the jobs
    # API, which is an environment fault, and before this entry it reached
    # RUNNER_INFRA only by falling through the default -- the right code for
    # the wrong reason, and indistinguishable from an unrecognised step.
    "poll run jobs",
)

# Job-STEP name substrings that identify a GOVERNANCE GATE refusing because a
# required evidence artifact is missing or not yet durable (OMN-18902). This is
# the third cause, and on this fleet it is the dominant one: 118 of 141
# measured failing steps match this tuple.
#
# Every entry below is present in the measured corpus. They are ordinary gate
# vocabulary rather than one-off step names, with two deliberate exceptions:
#
#   ``poll run jobs`` is NOT here -- it is infra, above.
#   ``check for integration tests`` IS spelled out, because the coverage gate's
#   step name carries no gate vocabulary at all and its only distinctive
#   substring, ``test``, is a PRODUCT token. Naming the step is the honest
#   option; guessing from ``test`` would put it in the wrong class.
_PROCESS_GATE_STEP_SUBSTRINGS: tuple[str, ...] = (
    "evidence",
    "occ",
    "receipt",
    "companion",
    # NOT a bare "gate". The word is too common to carry a diagnosis: the
    # fixture corpus's own fail-closed control step is named "Some unknown
    # gate", and a product step called "Run lint gate" would be swallowed the
    # same way. The three measured gate steps are named instead, and
    # "Run Receipt-Gate" is matched by "receipt" above rather than by either.
    "evaluate gate",
    "deploy gate",
    "bump",
    "base branch",
    "auto-merge",
    "adversarial",
    "hostile",
    "assertions",
    "reason graph",
    "product readiness",
    "pr-merged",
    "imperative contract",
    "check for integration tests",
)

# Job-STEP name substrings that identify an affirmative PRODUCT failure. Only a
# failed step matching one of these — with a real ``failure`` conclusion — may be
# classified ``PRODUCT_FAILED``. Mirrors the orchestrator
# ``_CODE_SIGNAL_CHECK_SUBSTRINGS`` vocabulary.
_PRODUCT_STEP_SUBSTRINGS: tuple[str, ...] = (
    "lint",
    "ruff",
    "mypy",
    "type-check",
    "typecheck",
    "type check",
    "type safety",
    "test",
    "pytest",
    "format",
    "compile",
    "build",
    "coverage",
    "pre-commit",
    "precommit",
    # OMN-18902, from the measured corpus: a shard-refusal step is about the
    # test suite, not about a governance artifact. Kept distinct from the bare
    # "test" token, which already matches, so this entry documents rather than
    # widens.
    "test shard",
)

# Job-LOG substrings indicating the GitHub API/metadata call itself failed
# (HTML/503/rate-limit/DNS on api.github.com) — an outage, not a product red
# (F-07). Kept distinct from the runner-infra signatures so the controller can
# withhold REST-dependent mutation on an outage while merely rerunning on runner
# infra.
_API_OUTAGE_LOG_SIGNATURES: tuple[str, ...] = (
    "empty reply from server",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway time-out",
    "<!doctype html>",
    "<html",
    "you have exceeded a secondary rate limit",
    "api rate limit exceeded",
    "could not resolve host: api.github.com",
    "server error: `502",
    "server error: `503",
)

# Job-LOG substrings indicating a self-hosted runner network/clone/container
# infrastructure fault (F-08). This module is the SINGLE SOURCE OF TRUTH for the
# signatures the classifier keys on: the inventory node imports the public
# ``ALL_LOG_SIGNATURES`` union (below) to drive its log extraction, so the tuple
# it feeds ``classify`` is exactly what ``classify`` matches on. A node-local
# subset previously drifted from this set and silently dropped the F-23
# isolation-hang and every API-outage signature (OMN-14769 follow-up to
# OMN-14765) — do not re-introduce a divergent copy in any consumer.
_RUNNER_INFRA_LOG_SIGNATURES: tuple[str, ...] = (
    "could not resolve host: github.com",
    "could not resolve host: codeload.github.com",
    "gnutls recv error",
    "rpc failed; curl 56",
    "fatal: early eof",
    "unexpected disconnect while reading sideband packet",
    "invalid index-pack output",
    "failed to initialize container",
    "one or more containers failed to start",
    "service container postgres failed",
    "failed to prepare extraction snapshot",
    "lease does not exist",
    "failed to lookup address information",
    "temporary failure in name resolution",
    "failed to download distribution due to network timeout",
    "request failed after 3 retries",
    "the runner has received a shutdown signal",
    "lost communication with the server",
    # F-23: runner hard-exit / thread-isolation hang masquerading as a product
    # failure. The classifier only TAGS the hang as RUNNER_INFRA; the actual
    # os._exit -> signal-timeout mechanism fix is a separate infra change.
    "os._exit(1)",
    "thread timeout",
    "hard timeout",
    "leaked thread",
    # OMN-18820: the slice's interpreter died on a fatal signal AFTER its own
    # summary reported a complete, zero-failure session (a C-extension crash in
    # garbage collection at shutdown). Ranking this above PRODUCT_FAILED is safe
    # even though infra outranks product here: `run_shadow_slice` emits this
    # exact phrase ONLY when the summary showed zero failures and zero errors, so
    # a run with a failing test cannot carry it. A bare "segmentation fault"
    # signature would NOT be safe, and is deliberately absent — it would flip
    # the runs that segfault *and* fail tests from red to green.
    "runner interpreter crashed after a clean session",
)

_CANCELLED_CONCLUSIONS: frozenset[str] = frozenset(
    {"cancelled", "canceled", "timed_out"}
)
_PRODUCT_FAIL_CONCLUSIONS: frozenset[str] = frozenset({"failure", "action_required"})

# Deterministic precedence used by ``dominant_reason_code`` to collapse a PR's
# per-check reason codes into one PR-level diagnosis (lower index = wins).
# OMN-18902: PROCESS_GATE_REFUSED sits between STALE_CONTEXT and RUNNER_INFRA.
# It is BELOW product because a real code failure must still be fixed even when
# a gate also refused, and ABOVE infra because "a required artifact is missing"
# is a specific, actionable diagnosis and "a machine broke" is the fallback.
#
# This tuple is a LITERAL enumeration, not an iteration over the enum, so a
# member absent from it does not merely rank last -- it falls past the loop
# entirely and is relabelled RUNNER_INFRA by the catch-all below. That is why
# ``test_every_enum_member_has_a_precedence_entry`` exists.
_REASON_CODE_PRECEDENCE: tuple[EnumMergeCheckReasonCode, ...] = (
    EnumMergeCheckReasonCode.PRODUCT_FAILED,
    EnumMergeCheckReasonCode.GITHUB_API_OUTAGE,
    EnumMergeCheckReasonCode.STALE_CONTEXT,
    EnumMergeCheckReasonCode.PROCESS_GATE_REFUSED,
    EnumMergeCheckReasonCode.RUNNER_INFRA,
    EnumMergeCheckReasonCode.CANCELLED,
)

# Public: the COMPLETE set of job-LOG signatures the classifier keys on, across
# both families. The inventory node scans job logs for exactly these so its
# extracted ``log_signatures`` tuple is precisely what ``classify`` matches on —
# one source of truth, no drift (OMN-14769). Order is runner-infra first, then
# API-outage; callers that preserve match order therefore surface an infra
# signature ahead of an outage one, which is harmless (the classifier ranks by
# family, not by list position).
ALL_LOG_SIGNATURES: tuple[str, ...] = (
    *_RUNNER_INFRA_LOG_SIGNATURES,
    *_API_OUTAGE_LOG_SIGNATURES,
)


def text_has_api_outage_signature(text: str) -> bool:
    """True if ``text`` carries a GitHub API-outage signature (F-07).

    ``text`` may be a job-log body or a gh error/metadata blob. Used by the
    inventory node to set ``api_error`` when the jobs-API metadata call itself
    returns an outage body (HTML error page / 5xx / rate-limit) rather than the
    expected JSON, so the classifier emits ``GITHUB_API_OUTAGE`` (withhold)
    instead of failing closed to an infra rerun.
    """
    lowered = text.lower()
    return any(sig in lowered for sig in _API_OUTAGE_LOG_SIGNATURES)


@dataclass(frozen=True)
class MergeCheckFacts:
    """Jobs-API attempt facts for a single required check (the classifier input).

    All fields are resolved by the caller (inventory node / merge_sweep) from
    ``gh api repos/<repo>/actions/runs/<run_id>/jobs``; this module performs no
    I/O. The tuple ``(pr_number, current_head_sha, required_context, run_id,
    attempt, run_event)`` is the identity the diagnosis is keyed on.
    """

    # Identity / provenance.
    pr_number: int | None = None
    required_context: bool = True
    run_id: str | None = None
    attempt: int | None = None
    run_event: str | None = None
    head_sha: str | None = None
    current_head_sha: str | None = None
    # Whether a newer attempt/run supersedes this one for the same context.
    is_superseded: bool = False
    # Whether the jobs-API metadata call itself failed (HTML/503/timeout).
    api_error: bool = False
    # OMN-18902: whether a LATER run attempt of this same, unchanged head SHA
    # reached a successful conclusion. The purest infra signal available --
    # nothing about the product changed between the two attempts -- and it
    # outranks every step-name match. Resolved by the caller from the runs API
    # (``run_attempt`` crossed with ``conclusion`` for one ``head_sha``).
    #
    # Defaults False, so a caller that does not resolve it gets exactly the
    # verdict it got before this field existed.
    same_sha_later_attempt_succeeded: bool = False
    # Job outcome.
    job_status: str | None = None
    job_conclusion: str | None = None
    failed_step_name: str | None = None
    # Lower-cased job-log infra/outage signatures already extracted by the caller.
    log_signatures: tuple[str, ...] = field(default_factory=tuple)


def _matches_any(haystacks: tuple[str, ...], needles: tuple[str, ...]) -> bool:
    lowered = tuple(h.lower() for h in haystacks if h)
    return any(needle in h for h in lowered for needle in needles)


def _step_is_infra(step: str) -> bool:
    return any(sub in step for sub in _RUNNER_INFRA_STEP_SUBSTRINGS)


def _step_is_product(step: str) -> bool:
    return any(sub in step for sub in _PRODUCT_STEP_SUBSTRINGS)


def _step_is_process_gate(step: str) -> bool:
    return any(sub in step for sub in _PROCESS_GATE_STEP_SUBSTRINGS)


class EnumCiAttemptCauseClass(StrEnum):
    """The four-way cause class the attempts metric reports (OMN-18902).

    A reporting view over :class:`EnumMergeCheckReasonCode`, not a second
    diagnosis. ``UNKNOWN`` is the one that could not be derived from the code
    alone: ``RUNNER_INFRA`` is returned both for an affirmatively identified
    environment fault and for a step nothing recognised, and the metric's
    stopping rule is written on the second of those. See
    :func:`eval_cause_class`, which takes the VERDICT rather than the code
    precisely because the code cannot answer it.
    """

    WORK = "work"
    PROCESS = "process"
    INFRA = "infra"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MergeCheckVerdict:
    """A reason code plus how the classifier arrived at it (OMN-18902).

    ``affirmative`` is ``False`` on exactly one path: the fail-closed default,
    where a ``failure`` carried no signal any vocabulary recognised. Every
    other return sets it ``True``.

    It exists because the bare code cannot distinguish "this is infra, and I
    can say which infra" from "I have no idea, and the safe answer is infra".
    Both were ``RUNNER_INFRA``, so an unrecognised-share metric computed over
    codes is zero by construction and tells nobody anything.
    """

    code: EnumMergeCheckReasonCode
    affirmative: bool


def eval_cause_class(verdict: MergeCheckVerdict) -> EnumCiAttemptCauseClass:
    """Map one verdict to the four-way cause class the metric reports.

    Work-caused is ``PRODUCT_FAILED`` and nothing else. A fail-closed verdict
    is ``UNKNOWN`` whatever its code says, which is the whole point of taking
    a verdict here rather than a code.
    """
    if not verdict.affirmative:
        return EnumCiAttemptCauseClass.UNKNOWN
    if verdict.code is EnumMergeCheckReasonCode.PRODUCT_FAILED:
        return EnumCiAttemptCauseClass.WORK
    if verdict.code is EnumMergeCheckReasonCode.PROCESS_GATE_REFUSED:
        return EnumCiAttemptCauseClass.PROCESS
    return EnumCiAttemptCauseClass.INFRA


def classify_verdict(facts: MergeCheckFacts) -> MergeCheckVerdict:
    """Classify one failed/non-green required check, with its provenance.

    Precedence (highest first), fail-closed, as amended by OMN-18902:

    0. ``RUNNER_INFRA`` -- a LATER attempt of this same, unchanged head SHA
       reached green. Ahead of every step-name match: nothing about the
       product changed between the two attempts, so the step's own name is
       not evidence about the product whatever it says.
    1. ``STALE_CONTEXT`` -- superseded attempt, an old head SHA, or a
       non-PR-associated run event on a required context.
    2. ``GITHUB_API_OUTAGE`` -- the jobs-API/metadata call failed, or the log
       carries an API-outage signature.
    3. ``RUNNER_INFRA`` -- the failed step is a runner/env setup step, or the
       log matches a network/clone/isolation-hang infra signature.
    4. ``PROCESS_GATE_REFUSED`` -- a real ``failure`` conclusion on a
       governance-gate step. Above product because two measured gate steps
       carry the ``test`` product token and a code fix is the wrong remedy for
       a missing artifact.
    5. ``CANCELLED`` -- a ``cancelled``/``timed_out`` conclusion with no
       product failure identified. A CANCELLED gate step lands here, not at 4,
       because branch 4 requires a failure conclusion: a gate that was
       cancelled never refused anything.
    6. ``PRODUCT_FAILED`` -- a real ``failure`` conclusion on an identified
       product step.

    Anything else (a ``failure`` with no identified step of any family, an
    unknown conclusion) fails closed to ``RUNNER_INFRA`` -- never
    ``PRODUCT_FAILED`` -- and is the ONE path that reports
    ``affirmative=False``.
    """
    infra = EnumMergeCheckReasonCode.RUNNER_INFRA

    # 0. SAME-SHA RE-RUN RESCUE -- ahead of every step-name match (OMN-18902).
    if facts.same_sha_later_attempt_succeeded:
        return MergeCheckVerdict(code=infra, affirmative=True)

    # 1. STALE_CONTEXT -- a fix already landed; do not fix, refresh/supersede.
    stale = EnumMergeCheckReasonCode.STALE_CONTEXT
    if facts.is_superseded:
        return MergeCheckVerdict(code=stale, affirmative=True)
    if (
        facts.required_context
        and facts.run_event is not None
        and facts.run_event.strip().lower() not in _PR_ASSOCIATED_EVENTS
    ):
        return MergeCheckVerdict(code=stale, affirmative=True)
    if (
        facts.head_sha
        and facts.current_head_sha
        and facts.head_sha.strip().lower() != facts.current_head_sha.strip().lower()
    ):
        return MergeCheckVerdict(code=stale, affirmative=True)

    # 2. GITHUB_API_OUTAGE -- platform, not product.
    outage = EnumMergeCheckReasonCode.GITHUB_API_OUTAGE
    if facts.api_error:
        return MergeCheckVerdict(code=outage, affirmative=True)
    if _matches_any(facts.log_signatures, _API_OUTAGE_LOG_SIGNATURES):
        return MergeCheckVerdict(code=outage, affirmative=True)

    step = (facts.failed_step_name or "").strip().lower()
    conclusion = (facts.job_conclusion or "").strip().lower()

    # 3. RUNNER_INFRA -- provisioning/network/isolation, rerunnable. Ranked
    #    above CANCELLED so an infra-step cancellation (checkout killed) is
    #    infra, not a bare cancel, and above PRODUCT so an isolation-hang
    #    signature never reads as a product red (F-23).
    if step and _step_is_infra(step):
        return MergeCheckVerdict(code=infra, affirmative=True)
    if _matches_any(facts.log_signatures, _RUNNER_INFRA_LOG_SIGNATURES):
        return MergeCheckVerdict(code=infra, affirmative=True)

    # 4. PROCESS_GATE_REFUSED -- a governance gate refused a missing artifact.
    #    Gated on a real failure conclusion, so a cancelled gate falls to 5.
    if conclusion in _PRODUCT_FAIL_CONCLUSIONS and step and _step_is_process_gate(step):
        return MergeCheckVerdict(
            code=EnumMergeCheckReasonCode.PROCESS_GATE_REFUSED, affirmative=True
        )

    # 5. CANCELLED -- a clean cancellation/timeout that produced no product
    #    verdict. A product step that genuinely FAILED has conclusion=failure
    #    (branch 6), so this never masks a real product red.
    if conclusion in _CANCELLED_CONCLUSIONS:
        return MergeCheckVerdict(
            code=EnumMergeCheckReasonCode.CANCELLED, affirmative=True
        )

    # 6. PRODUCT_FAILED -- affirmative product-step failure only.
    if conclusion in _PRODUCT_FAIL_CONCLUSIONS and step and _step_is_product(step):
        return MergeCheckVerdict(
            code=EnumMergeCheckReasonCode.PRODUCT_FAILED, affirmative=True
        )

    # Fail closed: an indeterminate failure (a failure no vocabulary
    # recognised, or an unknown conclusion) is treated as infra, never a
    # product red. This is the one non-affirmative return.
    return MergeCheckVerdict(code=infra, affirmative=False)


def classify(facts: MergeCheckFacts) -> EnumMergeCheckReasonCode:
    """Classify a single failed/non-green required check into one reason code.

    The bare-code entrypoint, unchanged in signature and return type for every
    existing consumer. :func:`classify_verdict` is the same decision with its
    provenance attached; see that function for the precedence.
    """
    return classify_verdict(facts).code


def _failed_step_name(job: dict[str, Any]) -> str | None:
    """Return the name of the first non-successful step in a jobs-API job object.

    Prefers a ``failure`` step (the affirmative signal); falls back to the first
    ``timed_out``/``cancelled`` step so an infra-step cancellation is still
    attributable. Returns ``None`` when no step is non-green (e.g. the whole job
    was cancelled before any step ran).
    """
    steps = job.get("steps")
    if not isinstance(steps, list):
        return None
    failed: str | None = None
    fallback: str | None = None
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        conclusion = str(raw.get("conclusion") or "").strip().lower()
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        if conclusion == "failure" and failed is None:
            failed = name
        elif conclusion in _CANCELLED_CONCLUSIONS and fallback is None:
            fallback = name
    return failed if failed is not None else fallback


def facts_from_job(
    job: dict[str, Any],
    *,
    pr_number: int | None = None,
    run_event: str | None = None,
    current_head_sha: str | None = None,
    required_context: bool = True,
    api_error: bool = False,
    is_superseded: bool = False,
    log_signatures: tuple[str, ...] = (),
    same_sha_later_attempt_succeeded: bool = False,
) -> MergeCheckFacts:
    """Build :class:`MergeCheckFacts` from a GitHub jobs-API job object.

    ``job`` is one element of the ``jobs`` array returned by
    ``GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs`` (latest attempt).
    Its ``head_sha``, ``run_attempt``, ``status``, ``conclusion`` and ``steps``
    carry everything the classifier keys on; the run event and the PR's current
    head SHA are resolved by the caller and threaded in.
    """
    run_attempt = job.get("run_attempt")
    return MergeCheckFacts(
        pr_number=pr_number,
        required_context=required_context,
        run_id=(str(job.get("run_id")) if job.get("run_id") is not None else None),
        attempt=(int(run_attempt) if isinstance(run_attempt, int) else None),
        run_event=run_event,
        head_sha=(str(job.get("head_sha")) if job.get("head_sha") else None),
        current_head_sha=current_head_sha,
        is_superseded=is_superseded,
        api_error=api_error,
        job_status=(str(job.get("status")) if job.get("status") else None),
        job_conclusion=(str(job.get("conclusion")) if job.get("conclusion") else None),
        failed_step_name=_failed_step_name(job),
        log_signatures=tuple(log_signatures),
        same_sha_later_attempt_succeeded=same_sha_later_attempt_succeeded,
    )


def classify_job(
    job: dict[str, Any],
    **kwargs: Any,
) -> EnumMergeCheckReasonCode:
    """Convenience: :func:`facts_from_job` then :func:`classify`."""
    return classify(facts_from_job(job, **kwargs))


def classify_dict(payload: dict[str, Any]) -> EnumMergeCheckReasonCode:
    """Classify from a plain fixture/fact dict (used by the CI/pre-commit gate).

    The dict may carry a raw jobs-API ``job`` object plus resolved context, or a
    flat set of :class:`MergeCheckFacts` fields. This is the single entrypoint
    the fixture-corpus gate exercises, so the corpus proves the exact shape the
    live inventory node feeds in.
    """
    context: dict[str, Any] = {
        "pr_number": payload.get("pr_number"),
        "run_event": payload.get("run_event"),
        "current_head_sha": payload.get("current_head_sha"),
        "required_context": bool(payload.get("required_context", True)),
        "api_error": bool(payload.get("api_error", False)),
        "is_superseded": bool(payload.get("is_superseded", False)),
        "log_signatures": tuple(payload.get("log_signatures", ()) or ()),
        "same_sha_later_attempt_succeeded": bool(
            payload.get("same_sha_later_attempt_succeeded", False)
        ),
    }
    job = payload.get("job")
    if isinstance(job, dict):
        return classify_job(job, **context)
    # Flat facts form (no raw job object).
    return classify(
        MergeCheckFacts(
            job_status=payload.get("job_status"),
            job_conclusion=payload.get("job_conclusion"),
            failed_step_name=payload.get("failed_step_name"),
            attempt=payload.get("attempt"),
            run_id=(str(payload["run_id"]) if payload.get("run_id") else None),
            head_sha=payload.get("head_sha"),
            **context,
        )
    )


def dominant_reason_code(
    reason_codes: tuple[str, ...] | tuple[EnumMergeCheckReasonCode, ...],
) -> EnumMergeCheckReasonCode | None:
    """Collapse a PR's per-check reason codes into one PR-level diagnosis.

    Uses the fixed precedence in ``_REASON_CODE_PRECEDENCE``: a single
    ``PRODUCT_FAILED`` dominates (a real code failure must be fixed even if other
    checks are flaky), then ``GITHUB_API_OUTAGE`` (withhold), then
    ``STALE_CONTEXT``, then ``RUNNER_INFRA``, then ``CANCELLED``. Returns ``None``
    for an empty set.
    """
    present: set[str] = {str(code) for code in reason_codes if code}
    if not present:
        return None
    for code in _REASON_CODE_PRECEDENCE:
        if str(code) in present:
            return code
    # Any unrecognized code fails closed to RUNNER_INFRA (never PRODUCT_FAILED).
    return EnumMergeCheckReasonCode.RUNNER_INFRA


__all__: list[str] = [
    "ALL_LOG_SIGNATURES",
    "EnumCiAttemptCauseClass",
    "EnumMergeCheckReasonCode",
    "MergeCheckFacts",
    "MergeCheckVerdict",
    "classify",
    "classify_dict",
    "classify_job",
    "classify_verdict",
    "dominant_reason_code",
    "eval_cause_class",
    "facts_from_job",
    "text_has_api_outage_signature",
]
