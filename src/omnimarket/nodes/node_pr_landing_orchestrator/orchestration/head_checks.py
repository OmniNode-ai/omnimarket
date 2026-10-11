# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The head-check classifier port, wired to classify_head_checks (OMN-20866).

node_pr_lifecycle_triage_compute's ``classify_head_checks`` (OMN-19825,
OMN-19830) is the one per-check classifier. This adapter builds its facts from
one ``read_head_checks`` answer of the GitHub landing effect and the landing
row, and calls the triage compute's handler in process (plan section 5: the
orchestrator calls its pure nodes in process, never over the bus).

What the facts carry, and what they cannot:

* every check-run copy at the head, with its run id and, for a failed copy,
  the run attempt the effect read (F7);
* ``required`` from the base branch's required status contexts the effect read
  beside the check runs (classic protection and rulesets). An answer without
  them (an effect asked without a base) marks every check required, so a red
  anywhere blocks: stricter, never looser;
* a required context with no check run on the head yet is pending, decided
  here before the classifier runs, because the classifier judges only the
  copies it is given. A context GitHub reports only as a commit status reads
  as missing, so it stays pending (fail closed);
* the companion state from the row, the merge state from the newest PR read,
  and ``base_requires_up_to_date`` False: the effect does not read strictness,
  and the arm gate's merge-state criterion (CLEAN) refuses a behind head.
"""

from __future__ import annotations

from omnimarket.events.pr_head_check.enum_head_check_verdict import (
    EnumHeadCheckVerdict,
)
from omnimarket.events.pr_head_check.model_head_check_verdict import (
    ModelHeadCheckVerdict,
)
from omnimarket.events.pr_landing.enum_pr_landing_companion_status import (
    EnumPrLandingCompanionStatus,
)
from omnimarket.events.pr_landing_github.model_github_check_run_fact import (
    ModelGithubCheckRunFact,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_completed import (
    ModelPrLandingGithubCompleted,
)
from omnimarket.models.pr_head_check.enum_check_run_conclusion import (
    EnumCheckRunConclusion,
)
from omnimarket.models.pr_head_check.enum_check_run_status import (
    EnumCheckRunStatus,
)
from omnimarket.models.pr_head_check.enum_head_check_companion_state import (
    COMPANION_STATES_WITH_PR,
    EnumHeadCheckCompanionState,
)
from omnimarket.models.pr_head_check.enum_pr_merge_state import EnumPrMergeState
from omnimarket.models.pr_head_check.model_head_check_facts import (
    ModelHeadCheckFacts,
)
from omnimarket.models.pr_head_check.model_head_check_run import ModelHeadCheckRun
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_workflow_row import (
    ModelPrLandingWorkflowRow,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_head_checks import (
    HandlerClassifyHeadChecks,
)

_COMPANION_STATE: dict[EnumPrLandingCompanionStatus, EnumHeadCheckCompanionState] = {
    EnumPrLandingCompanionStatus.NONE: EnumHeadCheckCompanionState.NONE,
    # A command in flight has no companion PR yet.
    EnumPrLandingCompanionStatus.PENDING: EnumHeadCheckCompanionState.NONE,
    EnumPrLandingCompanionStatus.OPEN: EnumHeadCheckCompanionState.OPEN,
    EnumPrLandingCompanionStatus.CONFLICTING: EnumHeadCheckCompanionState.CONFLICTING,
    EnumPrLandingCompanionStatus.MERGED: EnumHeadCheckCompanionState.MERGED,
    EnumPrLandingCompanionStatus.CLOSED: EnumHeadCheckCompanionState.CLOSED,
    EnumPrLandingCompanionStatus.DECLINED: EnumHeadCheckCompanionState.DECLINED,
}


def _check_run(fact: ModelGithubCheckRunFact, *, required: bool) -> ModelHeadCheckRun:
    status = EnumCheckRunStatus(fact.status)
    completed = status is EnumCheckRunStatus.COMPLETED
    return ModelHeadCheckRun(
        name=fact.name,
        check_run_id=fact.check_run_id,
        status=status,
        conclusion=EnumCheckRunConclusion(fact.conclusion)
        if completed and fact.conclusion
        else None,
        app_slug=fact.app_slug,
        run_id=fact.run_id,
        run_attempt=fact.run_attempt,
        required=required,
    )


def _companion(
    row: ModelPrLandingWorkflowRow,
) -> tuple[EnumHeadCheckCompanionState, int | None]:
    landing = row.landing
    if landing is None:
        return EnumHeadCheckCompanionState.NONE, None
    state = _COMPANION_STATE[landing.companion.status]
    occ_pr = landing.companion.occ_pr
    if state in COMPANION_STATES_WITH_PR:
        if occ_pr is None:
            return EnumHeadCheckCompanionState.NONE, None
        return state, occ_pr
    return state, None


def _merge_state(row: ModelPrLandingWorkflowRow) -> EnumPrMergeState:
    raw = (row.merge_state_status or "").lower()
    try:
        return EnumPrMergeState(raw)
    except ValueError:
        return EnumPrMergeState.UNKNOWN


def head_check_facts(
    completed: ModelPrLandingGithubCompleted, row: ModelPrLandingWorkflowRow
) -> ModelHeadCheckFacts:
    """The classifier's facts for one read_head_checks answer."""
    if completed.head_sha is None:
        msg = f"a read_head_checks answer for {row.landing_key} names no head"
        raise ValueError(msg)
    required = completed.required_contexts
    companion_state, companion_pr = _companion(row)
    return ModelHeadCheckFacts(
        repository=completed.repository,
        pr_number=completed.pr_number,
        head_sha=completed.head_sha,
        base_ref=row.base_ref or "unknown",
        observed_at=row.head_checks_read_at or row.updated_at,
        checks=tuple(
            _check_run(fact, required=required is None or fact.name in required)
            for fact in completed.check_runs
        ),
        companion_state=companion_state,
        companion_pr=companion_pr,
        merge_state=_merge_state(row),
        base_requires_up_to_date=False,
    )


def missing_required_contexts(completed: ModelPrLandingGithubCompleted) -> list[str]:
    """Required contexts with no check run on the head, sorted."""
    if completed.required_contexts is None:
        return []
    present = {fact.name for fact in completed.check_runs}
    return sorted(set(completed.required_contexts) - present)


class TriageHeadCheckClassifier:
    """The landing orchestrator's head-check classifier: classify_head_checks."""

    def __init__(self, handler: HandlerClassifyHeadChecks | None = None) -> None:
        self._handler = handler if handler is not None else HandlerClassifyHeadChecks()

    async def classify(
        self,
        completed: ModelPrLandingGithubCompleted,
        row: ModelPrLandingWorkflowRow,
    ) -> ModelHeadCheckVerdict:
        facts = head_check_facts(completed, row)
        if missing_required_contexts(completed):
            return ModelHeadCheckVerdict(
                repository=facts.repository,
                pr_number=facts.pr_number,
                head_sha=facts.head_sha,
                verdict=EnumHeadCheckVerdict.PENDING,
            )
        return await self._handler.handle(facts)


__all__: list[str] = [
    "TriageHeadCheckClassifier",
    "head_check_facts",
    "missing_required_contexts",
]
