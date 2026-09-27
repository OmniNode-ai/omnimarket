# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerClassifyHeadChecks: the ``classify_head_checks`` operation.

A pure definition-B compute over one PR head's check facts, returning the
PR-level verdict the landing workflow's reducer acts on while a PR sits in
CHECKS_PENDING. It reads no clock, no network and no file; the facts were
read beforehand by an effect.

How a verdict is reached
------------------------
1. **The newest copy of each check name decides it.** A head can carry many
   copies of one name (a re-run, or one per caller workflow); like branch
   protection, only the newest by ``started_at`` then ``check_run_id`` counts.
2. **Blocking checks** are the base's required contexts, plus every check a
   red aggregate (CI Summary) names as failed, missing or pending in its own
   report. Nothing else decides a verdict.
3. **Each red blocking check gets its reason code** from
   ``omnimarket.merge_control.reason_code_classifier``, the one per-check
   vocabulary, fed the failed step, the conclusion and the failure
   annotations. Those codes are reported as ``check_reasons``.
4. **Each red is then put in one class** that says what clears it:

   - change control: a change-control gate (every ``occ-preflight /
     eligibility`` copy, the Companion Merged Gate, the Receipt Gate's
     ``verify / verify``, OCC Preflight Dependency); an aggregate whose report
     names one; or a shard refusal that only reports the shards were skipped
     behind an unresolved change-control gate;
   - timed out: a ``timed_out`` conclusion, a job over its time limit, or the
     CI Summary poll deadline;
   - runner: the runner or its environment failed, as the reason classifier
     affirmatively found it (``runner_infra``), or GitHub's API failed
     (``github_api_outage``);
   - cancelled: a clean cancellation;
   - product: everything else, including a failure the reason classifier
     could only fail closed on, a stale context, and a governance gate that
     refused outside change control. A red nothing recognises is handed to
     an agent, never re-run blindly (safety property P2).

5. **The verdict is the first that holds**, in the corpus manifest's
   precedence: product_failed, behind_required, change_control_open,
   pending, stale_caller_pin, change_control_stale, timed_out, runner_infra,
   cancelled, green. A re-run verdict names every red blocking check, sorted.
   change_control_open is kept (revision 1, R3): the reducer has a row for it.
6. **Every blocking result carries its run attempt** where the facts hold one
   (revision 1, F7), so the reducer can treat a result older than the attempt
   its last re-run started as pending, not as a second failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from omnimarket.merge_control.reason_code_classifier import (
    EnumMergeCheckReasonCode,
    MergeCheckFacts,
    MergeCheckVerdict,
    classify_verdict,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_check_run_conclusion import (
    CHECK_RUN_PASSING_CONCLUSIONS,
    EnumCheckRunConclusion,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_check_run_status import (
    EnumCheckRunStatus,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_companion_state import (
    EnumHeadCheckCompanionState,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    HEAD_CHECK_RERUN_VERDICTS,
    EnumHeadCheckVerdict,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_pr_merge_state import (
    EnumPrMergeState,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_attempt import (
    ModelHeadCheckAttempt,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_facts import (
    ModelHeadCheckFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_reason import (
    ModelHeadCheckReason,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_run import (
    ModelHeadCheckRun,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_verdict import (
    ModelHeadCheckVerdict,
)

# The change-control gates, by check name. The preflight runs once per caller
# workflow, so its copies carry a caller prefix ("call-reject-skip-token /
# occ-preflight / eligibility"); every copy is the same gate.
_CHANGE_CONTROL_NAME_SUFFIXES: tuple[str, ...] = ("occ-preflight / eligibility",)
_CHANGE_CONTROL_NAME_PREFIXES: tuple[str, ...] = (
    "OCC Companion Merged Gate",
    "OCC Preflight Dependency",
)
_CHANGE_CONTROL_NAMES: frozenset[str] = frozenset({"verify / verify"})

# A shard refusal that reports only that the test shards were skipped: the
# coverage census refuses when detect-changes was skipped behind a gate. It
# follows whatever held the shards back and says nothing about the product.
_SHARD_REFUSAL_SIGNATURES: tuple[str, ...] = (
    "refuse when the test shards did not run",
    "detect-changes=skipped",
)

# A job that ran out of time, as Actions and the CI Summary poller report it.
# Actions reports a job-level time-out as conclusion cancelled with the first
# annotation; the CI Summary poller fails at its own deadline.
_TIME_LIMIT_SIGNATURES: tuple[str, ...] = (
    "exceeded the maximum execution time",
    "ci summary poll deadline",
)

_NO_START = datetime.min.replace(tzinfo=UTC)


class _EnumRedClass(StrEnum):
    """What clears one red blocking check."""

    CHANGE_CONTROL = "change_control"
    TIMED_OUT = "timed_out"
    RUNNER = "runner"
    CANCELLED = "cancelled"
    PRODUCT = "product"


def is_change_control_check(name: str) -> bool:
    """Whether ``name`` is one of the change-control gates."""
    return (
        name in _CHANGE_CONTROL_NAMES
        or name.endswith(_CHANGE_CONTROL_NAME_SUFFIXES)
        or name.startswith(_CHANGE_CONTROL_NAME_PREFIXES)
    )


def _newest_copies(facts: ModelHeadCheckFacts) -> dict[str, ModelHeadCheckRun]:
    newest: dict[str, ModelHeadCheckRun] = {}
    for check in facts.checks:
        held = newest.get(check.name)
        if held is None or _order(check) > _order(held):
            newest[check.name] = check
    return newest


def _order(check: ModelHeadCheckRun) -> tuple[datetime, int]:
    return (check.started_at or _NO_START, check.check_run_id)


def _is_red(check: ModelHeadCheckRun) -> bool:
    return (
        check.status is EnumCheckRunStatus.COMPLETED
        and check.conclusion is not None
        and check.conclusion not in CHECK_RUN_PASSING_CONCLUSIONS
    )


def _blocking_names(
    facts: ModelHeadCheckFacts, newest: dict[str, ModelHeadCheckRun]
) -> set[str]:
    required = {check.name for check in facts.checks if check.required}
    named: set[str] = set()
    for name in required:
        check = newest[name]
        if _is_red(check):
            named.update(b for b in check.named_blockers if b in newest)
    return required | named


def _reason(check: ModelHeadCheckRun) -> MergeCheckVerdict:
    return classify_verdict(
        MergeCheckFacts(
            required_context=check.required,
            run_id=str(check.run_id) if check.run_id is not None else None,
            attempt=check.run_attempt,
            run_event=check.run_event,
            job_status=check.status.value,
            job_conclusion=check.conclusion.value if check.conclusion else None,
            failed_step_name=check.failed_step,
            log_signatures=tuple(a.lower() for a in check.annotations),
        )
    )


def _failure_text(check: ModelHeadCheckRun) -> str:
    return " ".join((check.failed_step or "", *check.annotations)).lower()


def _red_class(
    check: ModelHeadCheckRun,
    reason: MergeCheckVerdict,
    change_control_unresolved: bool,
) -> _EnumRedClass:
    if is_change_control_check(check.name):
        return _EnumRedClass.CHANGE_CONTROL
    text = _failure_text(check)
    if change_control_unresolved and any(s in text for s in _SHARD_REFUSAL_SIGNATURES):
        return _EnumRedClass.CHANGE_CONTROL
    if any(is_change_control_check(name) for name in check.named_blockers):
        return _EnumRedClass.CHANGE_CONTROL
    if check.conclusion is EnumCheckRunConclusion.TIMED_OUT or any(
        s in text for s in _TIME_LIMIT_SIGNATURES
    ):
        return _EnumRedClass.TIMED_OUT
    code = reason.code
    if not reason.affirmative:
        return _EnumRedClass.PRODUCT
    if code in (
        EnumMergeCheckReasonCode.RUNNER_INFRA,
        EnumMergeCheckReasonCode.GITHUB_API_OUTAGE,
    ):
        return _EnumRedClass.RUNNER
    if code is EnumMergeCheckReasonCode.CANCELLED:
        return _EnumRedClass.CANCELLED
    return _EnumRedClass.PRODUCT


def classify_head_checks(facts: ModelHeadCheckFacts) -> ModelHeadCheckVerdict:
    """Classify one head's check facts into its verdict. Pure."""
    newest = _newest_copies(facts)
    blocking = sorted(_blocking_names(facts, newest))
    pending = [
        n for n in blocking if newest[n].status is not EnumCheckRunStatus.COMPLETED
    ]
    reds = {n: newest[n] for n in blocking if _is_red(newest[n])}
    reasons = {n: _reason(check) for n, check in reds.items()}

    change_control_unresolved = any(
        is_change_control_check(n) for n in (*reds, *pending)
    )
    classes = {
        n: _red_class(check, reasons[n], change_control_unresolved)
        for n, check in reds.items()
    }
    verdict = _verdict(facts, classes, reds, pending)
    rerun = tuple(sorted(reds)) if verdict in HEAD_CHECK_RERUN_VERDICTS else ()
    return ModelHeadCheckVerdict(
        repository=facts.repository,
        pr_number=facts.pr_number,
        head_sha=facts.head_sha,
        verdict=verdict,
        rerun_checks=rerun,
        check_reasons=tuple(
            ModelHeadCheckReason(name=n, reason_code=reasons[n].code)
            for n in sorted(reds)
        ),
        check_attempts=tuple(
            ModelHeadCheckAttempt(check=n, attempt=attempt)
            for n in blocking
            if (attempt := newest[n].run_attempt) is not None
        ),
    )


def _verdict(
    facts: ModelHeadCheckFacts,
    classes: dict[str, _EnumRedClass],
    reds: dict[str, ModelHeadCheckRun],
    pending: list[str],
) -> EnumHeadCheckVerdict:
    present = set(classes.values())
    if _EnumRedClass.PRODUCT in present:
        return EnumHeadCheckVerdict.PRODUCT_FAILED
    if facts.base_requires_up_to_date and facts.merge_state is EnumPrMergeState.BEHIND:
        return EnumHeadCheckVerdict.BEHIND_REQUIRED
    companion_merged = facts.companion_state is EnumHeadCheckCompanionState.MERGED
    change_control_blocks = _EnumRedClass.CHANGE_CONTROL in present or any(
        is_change_control_check(n) for n in pending
    )
    if not companion_merged and change_control_blocks:
        return EnumHeadCheckVerdict.CHANGE_CONTROL_OPEN
    if pending:
        return EnumHeadCheckVerdict.PENDING
    if any(check.caller_changed_on_base for check in reds.values()):
        return EnumHeadCheckVerdict.STALE_CALLER_PIN
    if present == {_EnumRedClass.CHANGE_CONTROL}:
        return EnumHeadCheckVerdict.CHANGE_CONTROL_STALE
    for red_class, verdict in (
        (_EnumRedClass.TIMED_OUT, EnumHeadCheckVerdict.TIMED_OUT),
        (_EnumRedClass.RUNNER, EnumHeadCheckVerdict.RUNNER_INFRA),
        (_EnumRedClass.CANCELLED, EnumHeadCheckVerdict.CANCELLED),
    ):
        if red_class in present:
            return verdict
    return EnumHeadCheckVerdict.GREEN


class HandlerClassifyHeadChecks:
    """Classifies one head's check facts into one verdict."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(self, request: ModelHeadCheckFacts) -> ModelHeadCheckVerdict:
        """Return the verdict for ``request``'s head."""
        return classify_head_checks(request)


__all__: list[str] = [
    "HandlerClassifyHeadChecks",
    "classify_head_checks",
    "is_change_control_check",
]
