# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelLandingDecision: the output of the landing decision, and the typed worker briefs.

A decision is the actions of one tick, the outcomes it recorded, what set
DEGRADED, and the next controller state. The controller writes
``next_state`` first and then performs ``actions`` in order; the actions of
a repo in observe-only are returned in ``observed_actions`` and never
performed.

A worker brief is fixed per class: the text a worker receives is chosen by
the class and never composed by a model. The brief names the four outcomes a
result may carry and the push rule its evidence is verified against. A cause
brief is the same idea for one shared cause: fixed text, the cause's (check,
signature, example) pairs and its members, and the three results it may carry.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingBriefClass,
    EnumLandingCompanionVerdict,
    EnumLandingDegradedReason,
    EnumLandingEngine,
    EnumLandingGateReason,
    EnumLandingLandSkipReason,
    EnumLandingOutcome,
    EnumLandingOutcomeReason,
    EnumLandingResultKind,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    CAUSE_KEY_PATTERN,
    KEY_PATTERN,
    PR_KEY_PATTERN,
    REPO_PATTERN,
    SHA_PATTERN,
    SUBJECT_PATTERN,
    ModelLandingCausePair,
    ModelLandingControllerState,
    ModelLandingMemberRef,
)

VALID_RESULT_KINDS: Final[tuple[EnumLandingResultKind, ...]] = (
    EnumLandingResultKind.MERGED,
    EnumLandingResultKind.ARMED,
    EnumLandingResultKind.FIX_SUBMITTED,
    EnumLandingResultKind.EXTERNAL_BLOCKER,
)

PUSH_RULE: Final[str] = (
    "Push only with a plain fast-forward push, or with "
    "--force-with-lease=<ref>:<expected_old_head> naming the old head explicitly. "
    "Never push with a bare --force. Report every push in push_evidence, in order, "
    "as {ref, expected_old_head, new_head, mode}; the first expected_old_head is "
    "the head you were dispatched on or a head you last saw, and the last new_head "
    "is the head you report."
)

RESULT_RULE: Final[str] = (
    "Write exactly one result file, with exactly one of: merged; armed (auto-merge "
    "armed on the exact head after your last push); fix_submitted{head_sha, "
    "fix_attempt} with push_evidence or a rerun run id created after dispatch; "
    "external_blocker{kind, ref} with kind one of operator_decision, "
    "production_gate, hold_row, collaborator, upstream_open. waiting_ci, "
    "waiting_order, report_only and a bare blocked are invalid results. Do not "
    "write the ledger and do not choose other work."
)

CAUSE_RESULT_KINDS: Final[tuple[EnumLandingResultKind, ...]] = (
    EnumLandingResultKind.CAUSE_FIX_SUBMITTED,
    EnumLandingResultKind.CAUSE_NOT_SHARED,
    EnumLandingResultKind.EXTERNAL_BLOCKER,
)

CAUSE_PUSH_RULE: Final[str] = (
    "Fix once at the source, in one pull request in the source repository. Never "
    "push to a member PR's branch; list the members that need their own change "
    "in the fix PR's body instead."
)

CAUSE_RESULT_RULE: Final[str] = (
    "Write exactly one result file, with exactly one of: cause_fix_submitted"
    "{fix_ref, fix_head} naming the one fix PR and its head; cause_not_shared"
    "{reason} when the members only share an annotation by coincidence; "
    "external_blocker{kind, ref} with kind one of operator_decision, "
    "production_gate, hold_row, collaborator, upstream_open. Any other result is "
    "invalid and spends the attempt. Do not write the ledger and do not choose "
    "other work."
)

BRIEF_INSTRUCTIONS: Final[dict[EnumLandingBriefClass, str]] = {
    EnumLandingBriefClass.REAL_RED: (
        "A check on this head is red for a product reason, or stayed red after its "
        "one whole-run rerun. Read the failing check-run's log, fix the cause on the "
        "branch, run the focused tests for the change, and push."
    ),
    EnumLandingBriefClass.CASCADE_RED: (
        "This head is red because of a change-control cascade that its one rerun did "
        "not clear. Repair the change-control evidence the failing gate names, then "
        "push or rerun."
    ),
    EnumLandingBriefClass.BEHIND: (
        "This head is behind its base and the controller's update-branch did not "
        "make it current. Bring the branch up to date with its base and push."
    ),
    EnumLandingBriefClass.CONFLICT_MECHANICAL: (
        "This head conflicts with its base. Rebase onto the base, resolving only "
        "mechanical conflicts (imports, lockfiles, adjacent edits), and push with "
        "force-with-lease naming the head you rebased."
    ),
    EnumLandingBriefClass.CONFLICT_SUBSTANTIVE: (
        "This head conflicts with its base and an earlier mechanical attempt did not "
        "resolve it. Resolve the conflict preserving both sides' behaviour, run the "
        "focused tests, and push with force-with-lease naming the head you rebased."
    ),
    EnumLandingBriefClass.COMPANION_RED: (
        "This change-control companion is red while every member is eligible and at "
        "its stamped head. Fix the companion's evidence and push."
    ),
    EnumLandingBriefClass.COMPANION_ORPHAN: (
        "The controller closed this companion because no member is eligible or can "
        "become eligible, and it is still open. Close it with a comment naming why."
    ),
    EnumLandingBriefClass.RUNTIME: (
        "This runtime PR is red. Fix it and prove the fix on the lab runtime before "
        "pushing; name the lab readback in the result."
    ),
    EnumLandingBriefClass.SHARED_CAUSE: (
        "These PRs in one repository fail the same check with the same failure "
        "signature. In a detached worktree of the source repository at its base: "
        "read one member's failing job log and the base head's run of the same "
        "check; classify the source (the base head is red; the gate itself produces "
        "the message for PRs that did nothing wrong; a sibling pin; change-control "
        "companion contention, minimal unblock only; or not shared); fix it once at "
        "the source; draft code through the code delegation step and text through "
        "the landing text step."
    ),
}


class ModelLandingWorkerBrief(BaseModel):
    """The fixed brief one headless worker is dispatched with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brief_class: EnumLandingBriefClass
    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    head_sha: str = Field(..., pattern=SHA_PATTERN)
    lease_id: int = Field(..., ge=1)
    engine: EnumLandingEngine
    deadline_at: datetime
    allowed_results: tuple[EnumLandingResultKind, ...] = VALID_RESULT_KINDS
    push_rule: str = PUSH_RULE
    result_rule: str = RESULT_RULE
    instructions: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _fixed_text(self) -> ModelLandingWorkerBrief:
        if self.instructions != BRIEF_INSTRUCTIONS[self.brief_class]:
            raise ValueError("a brief's instructions are fixed by its class")
        if (
            self.allowed_results != VALID_RESULT_KINDS
            or self.push_rule != PUSH_RULE
            or self.result_rule != RESULT_RULE
        ):
            raise ValueError("a brief's result and push rules are fixed")
        return self


class ModelLandingCauseBrief(BaseModel):
    """The fixed brief one headless cause worker is dispatched with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brief_class: EnumLandingBriefClass = EnumLandingBriefClass.SHARED_CAUSE
    cause: str = Field(..., pattern=CAUSE_KEY_PATTERN)
    repo: str = Field(..., pattern=REPO_PATTERN)
    pairs: tuple[ModelLandingCausePair, ...] = ()
    members: tuple[ModelLandingMemberRef, ...] = Field(..., min_length=1)
    base_failing_checks: tuple[str, ...] = Field(
        default=(), description="The cause's checks that also fail on the base head."
    )
    attempt: int = Field(..., ge=1, description="This attempt's number in the episode.")
    lease_id: int = Field(..., ge=1)
    engine: EnumLandingEngine
    deadline_at: datetime
    allowed_results: tuple[EnumLandingResultKind, ...] = CAUSE_RESULT_KINDS
    push_rule: str = CAUSE_PUSH_RULE
    result_rule: str = CAUSE_RESULT_RULE
    instructions: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _fixed_text(self) -> ModelLandingCauseBrief:
        if self.brief_class is not EnumLandingBriefClass.SHARED_CAUSE:
            raise ValueError("a cause brief is the shared_cause class")
        if self.instructions != BRIEF_INSTRUCTIONS[self.brief_class]:
            raise ValueError("a brief's instructions are fixed by its class")
        if (
            self.allowed_results != CAUSE_RESULT_KINDS
            or self.push_rule != CAUSE_PUSH_RULE
            or self.result_rule != CAUSE_RESULT_RULE
        ):
            raise ValueError("a cause brief's result and push rules are fixed")
        return self


class ModelLandingAction(BaseModel):
    """One controller action. ``subject`` is the PR, companion or repo it acts on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumLandingActionKind
    subject: str = Field(..., min_length=1)
    head_sha: str | None = Field(
        default=None,
        pattern=SHA_PATTERN,
        description="merge is pinned to this head; rerun and update_branch act on it.",
    )
    lease_id: int | None = Field(default=None, ge=1)
    rebuild_key: str | None = Field(default=None, pattern=KEY_PATTERN)
    target_repo: str | None = Field(default=None, pattern=REPO_PATTERN)
    members: tuple[ModelLandingMemberRef, ...] = ()
    replacement: str | None = Field(default=None, pattern=PR_KEY_PATTERN)
    brief: ModelLandingWorkerBrief | None = None
    cause_brief: ModelLandingCauseBrief | None = None
    dedupe_key: str | None = Field(
        default=None,
        description="escalate_operator: <cause key>@<parked_until>, one per park.",
    )

    @model_validator(mode="after")
    def _shape(self) -> ModelLandingAction:
        kind = self.kind
        if kind is EnumLandingActionKind.MERGE and self.head_sha is None:
            raise ValueError("merge is always pinned to a head")
        if kind is EnumLandingActionKind.DISPATCH_WORKER and (
            (self.brief is None) == (self.cause_brief is None)
        ):
            raise ValueError("dispatch_worker carries exactly one brief")
        if kind is EnumLandingActionKind.ESCALATE_OPERATOR and (
            self.dedupe_key is None or not self.members
        ):
            raise ValueError("escalate_operator carries its dedupe key and members")
        if kind is EnumLandingActionKind.COMPANION_REBUILD and (
            self.rebuild_key is None or not self.members or self.target_repo is None
        ):
            raise ValueError("companion_rebuild carries K, its members and target repo")
        if (
            kind
            in (EnumLandingActionKind.KILL_WORKER, EnumLandingActionKind.DISCARD_RESULT)
            and self.lease_id is None
        ):
            raise ValueError(f"{kind.value} names its lease id")
        return self


class ModelLandingRecordedOutcome(BaseModel):
    """An outcome this tick recorded (for the ledger row and the scorecard).

    ``pr`` is the subject: a PR, or a cause key for a cause worker's outcome.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=SUBJECT_PATTERN)
    lease_id: int | None = Field(default=None, ge=1)
    outcome: EnumLandingOutcome
    reason: EnumLandingOutcomeReason = EnumLandingOutcomeReason.NONE
    blocker_fingerprint: str = ""


class ModelLandingDegraded(BaseModel):
    """One reason DEGRADED is set this tick, and what it names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: EnumLandingDegradedReason
    subject: str = Field(..., min_length=1)


class ModelLandingViolation(BaseModel):
    """A scorecard violation: a result that failed verification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=SUBJECT_PATTERN)
    lease_id: int = Field(..., ge=1)
    reason: EnumLandingOutcomeReason


class ModelLandingCompanionVerdictRow(BaseModel):
    """Which R2 rule applied to one open companion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    companion: str = Field(..., pattern=PR_KEY_PATTERN)
    verdict: EnumLandingCompanionVerdict


class ModelLandingGateRow(BaseModel):
    """One open PR under a gate suspension: the gates it names, and whether this
    tick released them (every reason's premise is a red head, the head is green
    and CLEAN, and the gate is older than ``stale_gate_seconds``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    head_sha: str = Field(..., pattern=SHA_PATTERN)
    reasons: tuple[EnumLandingGateReason, ...] = Field(..., min_length=1)
    since: datetime | None = None
    released: bool = False


class ModelLandingLandSkip(BaseModel):
    """Why one open, green, CLEAN PR got no merge on this head this tick."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    head_sha: str = Field(..., pattern=SHA_PATTERN)
    reason: EnumLandingLandSkipReason
    gate_reasons: tuple[EnumLandingGateReason, ...] = Field(
        default=(), description="The gates named, when the reason is gate."
    )

    @model_validator(mode="after")
    def _gate_is_named(self) -> ModelLandingLandSkip:
        if (self.reason is EnumLandingLandSkipReason.GATE) != bool(self.gate_reasons):
            raise ValueError("a gate skip names its gates, and only a gate skip does")
        return self


class ModelLandingDecision(BaseModel):
    """One tick's decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick: int = Field(..., ge=1)
    actions: tuple[ModelLandingAction, ...] = ()
    observed_actions: tuple[ModelLandingAction, ...] = Field(
        default=(),
        description="Actions withheld because their repo is observe-only (logged, not done).",
    )
    recorded_outcomes: tuple[ModelLandingRecordedOutcome, ...] = ()
    degraded: tuple[ModelLandingDegraded, ...] = ()
    violations: tuple[ModelLandingViolation, ...] = ()
    companion_verdicts: tuple[ModelLandingCompanionVerdictRow, ...] = ()
    observe_only_refused: tuple[str, ...] = Field(
        default=(), description="Draining repos refused observe-only this tick (P7)."
    )
    gates: tuple[ModelLandingGateRow, ...] = Field(
        default=(), description="Every open gate-suspended PR, with its named gates."
    )
    land_skips: tuple[ModelLandingLandSkip, ...] = Field(
        default=(),
        description=(
            "Every open, green, CLEAN PR with no merge on its head this tick, "
            "with the one reason why; such a PR has a merge or a row, never neither."
        ),
    )
    next_state: ModelLandingControllerState


__all__: list[str] = [
    "BRIEF_INSTRUCTIONS",
    "CAUSE_PUSH_RULE",
    "CAUSE_RESULT_KINDS",
    "CAUSE_RESULT_RULE",
    "PUSH_RULE",
    "RESULT_RULE",
    "VALID_RESULT_KINDS",
    "ModelLandingAction",
    "ModelLandingCauseBrief",
    "ModelLandingCompanionVerdictRow",
    "ModelLandingDecision",
    "ModelLandingDegraded",
    "ModelLandingGateRow",
    "ModelLandingLandSkip",
    "ModelLandingRecordedOutcome",
    "ModelLandingViolation",
    "ModelLandingWorkerBrief",
]
