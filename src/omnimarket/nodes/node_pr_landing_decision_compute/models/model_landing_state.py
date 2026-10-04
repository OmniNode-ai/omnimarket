# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller's own state, as the decision reads and returns it.

The controller is the single writer of this state. Each tick it passes the
state it last wrote in with the facts, and writes the ``next_state`` of the
decision back before it performs any action (write-ahead): a crash after the
write loses actions, never records. Every record is keyed by ``repo#pr``, and
a head is always a field, never part of a key (R7). A shared cause is keyed by
its cause key, ``cause:<owner>/<repo>:<signature>``, and its lease lives in the
same lease space as a PR's.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingBriefClass,
    EnumLandingEngine,
    EnumLandingOutcome,
    EnumLandingOutcomeReason,
    EnumLandingRebuildStatus,
)

PR_KEY_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$"
REPO_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
SHA_PATTERN = r"^[0-9a-f]{40}$"
KEY_PATTERN = r"^[0-9a-f]{64}$"
SIGNATURE_PATTERN = r"^[0-9a-f]{12}$"
CAUSE_KEY_PATTERN = r"^cause:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:[0-9a-f]{12}$"
SUBJECT_PATTERN = (
    r"^(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*"
    r"|cause:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:[0-9a-f]{12})$"
)


class ModelLandingMemberRef(BaseModel):
    """One companion member: ``repo#pr`` at the head its evidence was stamped for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    head_sha: str = Field(..., pattern=SHA_PATTERN)


class ModelLandingLease(BaseModel):
    """The one lease of one PR or one cause (R7). Released only by confirmed termination.

    ``pr`` is the subject: ``repo#pr``, or a cause key for a cause worker,
    whose dispatch head is the head of its first member.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=SUBJECT_PATTERN)
    lease_id: int = Field(..., ge=1)
    brief_class: EnumLandingBriefClass
    engine: EnumLandingEngine
    dispatched_at: datetime
    deadline_at: datetime
    dispatch_head: str = Field(..., pattern=SHA_PATTERN)
    seen_heads: tuple[str, ...] = Field(
        ...,
        min_length=1,
        description="Every head seen while the lease was live, dispatch head first.",
    )
    last_seen_head: str = Field(..., pattern=SHA_PATTERN)
    dispatch_red_checks: tuple[str, ...] = ()
    result_recorded_at: datetime | None = None
    kill_sent_tick: int | None = None
    revoked: bool = False
    stuck: bool = False


class ModelLandingPrRecord(BaseModel):
    """Everything the controller remembers about one PR between ticks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    outcome: EnumLandingOutcome | None = None
    reason: EnumLandingOutcomeReason = EnumLandingOutcomeReason.NONE
    blocker_fingerprint: str = Field(
        default="",
        description="The fingerprint recorded with the last outcome (R8).",
    )
    awaiting_head: str | None = Field(
        default=None,
        description="The head of a verified fix_submitted, owned by the controller until it lands.",
    )
    attempt_red_checks: tuple[str, ...] = Field(
        default=(),
        description="Checks that were red on the head the last fix attempt was dispatched on.",
    )
    ladder_index: int = Field(
        default=0, ge=0, description="Next engine on the R1 ladder."
    )
    parked_head: str | None = Field(
        default=None, description="Set while parked escalation_exhausted."
    )
    parked_fingerprint: str = ""
    rerun_heads: tuple[str, ...] = Field(
        default=(), description="Heads that already had their one whole-run rerun (R5)."
    )
    update_heads: tuple[str, ...] = Field(
        default=(), description="Heads the controller already updated from the base."
    )
    stale_refreshes: int = Field(
        default=0,
        ge=0,
        description="Update-branch refreshes sent for stale cancelled copies (bounded).",
    )


class ModelLandingRebuildRecord(BaseModel):
    """One companion rebuild request, keyed by its idempotency key K (R2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(..., pattern=KEY_PATTERN)
    target_repo: str = Field(..., pattern=REPO_PATTERN)
    members: tuple[ModelLandingMemberRef, ...] = Field(..., min_length=1)
    old_companion: str | None = Field(default=None, pattern=PR_KEY_PATTERN)
    status: EnumLandingRebuildStatus
    attempts: int = Field(..., ge=1)
    delivered_tick: int | None = None
    run_id: str | None = None
    next_retry_tick: int | None = None
    replacement: str | None = Field(default=None, pattern=PR_KEY_PATTERN)
    producer_release: str = ""


class ModelLandingUncovered(BaseModel):
    """A member a rebuild dropped while it was suspended (R2 rule 3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    target_repo: str = Field(..., pattern=REPO_PATTERN)


class ModelLandingEligibilityRerun(BaseModel):
    """A member owed one whole eligibility rerun after its companion merged."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    companion: str = Field(..., pattern=PR_KEY_PATTERN)


class ModelLandingCausePair(BaseModel):
    """One (check, signature) a shared cause carries, with one example annotation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str = Field(..., min_length=1)
    signature: str = Field(..., pattern=SIGNATURE_PATTERN)
    example: str = Field(
        default="", description="One member's annotation text for this pair."
    )
    members: int = Field(default=0, ge=0, description="PRs sharing the pair.")


class ModelLandingCauseRecord(BaseModel):
    """Everything the controller remembers about one shared cause between ticks.

    Upserted by cause key. ``attempts`` and ``spawn_failures`` count within one
    park episode; a park ends at ``parked_until`` or on a RELEASE row naming
    the cause, and the next episode starts from zero. ``escalated`` is the
    dedupe key of the one operator escalation of the current park.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(..., pattern=CAUSE_KEY_PATTERN)
    repo: str = Field(..., pattern=REPO_PATTERN)
    pairs: tuple[ModelLandingCausePair, ...] = ()
    members: tuple[ModelLandingMemberRef, ...] = Field(..., min_length=1)
    attempts: int = Field(default=0, ge=0)
    spawn_failures: int = Field(default=0, ge=0)
    outcome: EnumLandingOutcome | None = None
    reason: EnumLandingOutcomeReason = EnumLandingOutcomeReason.NONE
    fix_ref: str | None = Field(default=None, pattern=PR_KEY_PATTERN)
    fix_head: str | None = Field(default=None, pattern=SHA_PATTERN)
    rerun_at: datetime | None = Field(
        default=None, description="When the reruns after the fix merged were emitted."
    )
    rerun_heads: tuple[ModelLandingMemberRef, ...] = Field(
        default=(), description="Member heads already rerun for this cause."
    )
    parked_until: datetime | None = None
    escalated: str | None = Field(
        default=None, description="<cause key>@<parked_until> of the last escalation."
    )


class ModelLandingControllerState(BaseModel):
    """The controller's state file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    last_tick: int = Field(default=0, ge=0)
    next_lease_id: int = Field(default=1, ge=1)
    leases: tuple[ModelLandingLease, ...] = ()
    records: tuple[ModelLandingPrRecord, ...] = ()
    revoked_lease_ids: tuple[int, ...] = ()
    token_holder: str | None = Field(default=None, pattern=PR_KEY_PATTERN)
    draining_repos: tuple[str, ...] = ()
    observe_only_repos: tuple[str, ...] = ()
    rebuilds: tuple[ModelLandingRebuildRecord, ...] = ()
    uncovered: tuple[ModelLandingUncovered, ...] = ()
    eligibility_reruns: tuple[ModelLandingEligibilityRerun, ...] = ()
    close_requested: tuple[str, ...] = ()
    causes: tuple[ModelLandingCauseRecord, ...] = ()


__all__: list[str] = [
    "CAUSE_KEY_PATTERN",
    "KEY_PATTERN",
    "PR_KEY_PATTERN",
    "REPO_PATTERN",
    "SHA_PATTERN",
    "SIGNATURE_PATTERN",
    "SUBJECT_PATTERN",
    "ModelLandingCausePair",
    "ModelLandingCauseRecord",
    "ModelLandingControllerState",
    "ModelLandingEligibilityRerun",
    "ModelLandingLease",
    "ModelLandingMemberRef",
    "ModelLandingPrRecord",
    "ModelLandingRebuildRecord",
    "ModelLandingUncovered",
]
