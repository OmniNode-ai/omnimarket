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
  decides the diagnosis:
  ``STALE_CONTEXT > GITHUB_API_OUTAGE > RUNNER_INFRA > CANCELLED > PRODUCT_FAILED``.
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
    - ``PRODUCT_FAILED``: a product step (lint/type/test/coverage/build) failed
      with a real ``failure`` conclusion. Dispatch a code fix.
    """

    STALE_CONTEXT = "stale_context"
    GITHUB_API_OUTAGE = "github_api_outage"
    RUNNER_INFRA = "runner_infra"
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
# infrastructure fault (F-08). Superset of the inventory
# ``_CHECK_LOG_NETWORK_SIGNATURES`` set; kept local so the shared classifier does
# not import a node's private module (repo boundary rule).
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
)

_CANCELLED_CONCLUSIONS: frozenset[str] = frozenset(
    {"cancelled", "canceled", "timed_out"}
)
_PRODUCT_FAIL_CONCLUSIONS: frozenset[str] = frozenset({"failure", "action_required"})

# Deterministic precedence used by ``dominant_reason_code`` to collapse a PR's
# per-check reason codes into one PR-level diagnosis (lower index = wins).
_REASON_CODE_PRECEDENCE: tuple[EnumMergeCheckReasonCode, ...] = (
    EnumMergeCheckReasonCode.PRODUCT_FAILED,
    EnumMergeCheckReasonCode.GITHUB_API_OUTAGE,
    EnumMergeCheckReasonCode.STALE_CONTEXT,
    EnumMergeCheckReasonCode.RUNNER_INFRA,
    EnumMergeCheckReasonCode.CANCELLED,
)


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


def classify(facts: MergeCheckFacts) -> EnumMergeCheckReasonCode:
    """Classify a single failed/non-green required check into one reason code.

    Precedence (highest first), fail-closed:

    1. ``STALE_CONTEXT`` — superseded attempt, an old head SHA, or a
       non-PR-associated run event on a required context.
    2. ``GITHUB_API_OUTAGE`` — the jobs-API/metadata call failed, or the log
       carries an API-outage signature.
    3. ``RUNNER_INFRA`` — the failed step is a runner/env setup step, or the log
       matches a network/clone/isolation-hang infra signature.
    4. ``CANCELLED`` — a ``cancelled``/``timed_out`` conclusion with no product
       failure identified.
    5. ``PRODUCT_FAILED`` — a real ``failure`` conclusion on an identified
       product step.

    Anything else (a ``failure`` with no identified product step, an unknown
    conclusion) fails closed to ``RUNNER_INFRA`` — never ``PRODUCT_FAILED``.
    """
    # 1. STALE_CONTEXT — a fix already landed; do not fix, refresh/supersede.
    if facts.is_superseded:
        return EnumMergeCheckReasonCode.STALE_CONTEXT
    if (
        facts.required_context
        and facts.run_event is not None
        and facts.run_event.strip().lower() not in _PR_ASSOCIATED_EVENTS
    ):
        return EnumMergeCheckReasonCode.STALE_CONTEXT
    if (
        facts.head_sha
        and facts.current_head_sha
        and facts.head_sha.strip().lower() != facts.current_head_sha.strip().lower()
    ):
        return EnumMergeCheckReasonCode.STALE_CONTEXT

    # 2. GITHUB_API_OUTAGE — platform, not product.
    if facts.api_error:
        return EnumMergeCheckReasonCode.GITHUB_API_OUTAGE
    if _matches_any(facts.log_signatures, _API_OUTAGE_LOG_SIGNATURES):
        return EnumMergeCheckReasonCode.GITHUB_API_OUTAGE

    step = (facts.failed_step_name or "").strip().lower()
    conclusion = (facts.job_conclusion or "").strip().lower()

    # 3. RUNNER_INFRA — provisioning/network/isolation, rerunnable. Ranked above
    #    CANCELLED so an infra-step cancellation (checkout killed) is infra, not
    #    a bare cancel, and above PRODUCT so an isolation-hang signature never
    #    reads as a product red (F-23).
    if step and _step_is_infra(step):
        return EnumMergeCheckReasonCode.RUNNER_INFRA
    if _matches_any(facts.log_signatures, _RUNNER_INFRA_LOG_SIGNATURES):
        return EnumMergeCheckReasonCode.RUNNER_INFRA

    # 4. CANCELLED — a clean cancellation/timeout that produced no product
    #    verdict. A product step that genuinely FAILED has conclusion=failure
    #    (branch 5), so this never masks a real product red.
    if conclusion in _CANCELLED_CONCLUSIONS:
        return EnumMergeCheckReasonCode.CANCELLED

    # 5. PRODUCT_FAILED — affirmative product-step failure only.
    if conclusion in _PRODUCT_FAIL_CONCLUSIONS and step and _step_is_product(step):
        return EnumMergeCheckReasonCode.PRODUCT_FAILED

    # Fail closed: an indeterminate failure (failure with no identified product
    # step, or an unknown conclusion) is treated as infra, never a product red.
    return EnumMergeCheckReasonCode.RUNNER_INFRA


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
    "EnumMergeCheckReasonCode",
    "MergeCheckFacts",
    "classify",
    "classify_dict",
    "classify_job",
    "dominant_reason_code",
    "facts_from_job",
]
