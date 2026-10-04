# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closed vocabularies of the landing decision compute.

Every value here is a word the landing controller's model uses (the
``LandingController.tla`` model-before-build prerequisite of the landing
controller). A value that is not listed is refused at the model boundary, so a
new outcome, reason or action can only arrive by changing this file and the
tests that pin it.
"""

from __future__ import annotations

from enum import StrEnum


class EnumLandingPrState(StrEnum):
    """GitHub truth for one pull request."""

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class EnumLandingCi(StrEnum):
    """CI on the head, read over every check-run copy (no rollup).

    ``green`` means every check-run reporting on the head passed, required or
    not: there is no flaky-check exception.
    """

    GREEN = "green"
    RED = "red"
    PENDING = "pending"


class EnumLandingRedClass(StrEnum):
    """What clears a red head (the ci-watch classes).

    ``cascade``, ``replay``, ``cancelled_producer`` and ``runner_saturation``
    earn one whole-run rerun per head; ``product`` goes to a fix worker.
    """

    CASCADE = "cascade"
    REPLAY = "replay"
    CANCELLED_PRODUCER = "cancelled_producer"
    RUNNER_SATURATION = "runner_saturation"
    PRODUCT = "product"


RERUNNABLE_RED_CLASSES: frozenset[EnumLandingRedClass] = frozenset(
    {
        EnumLandingRedClass.CASCADE,
        EnumLandingRedClass.REPLAY,
        EnumLandingRedClass.CANCELLED_PRODUCER,
        EnumLandingRedClass.RUNNER_SATURATION,
    }
)


class EnumLandingMergeState(StrEnum):
    """The head's position against its base.

    ``blocked`` is GitHub's BLOCKED: a required context is missing or a ruleset
    holds the merge. A collector that does not send it sends ``unknown``, and
    ``blocked`` decides like ``unknown`` except for one case: with no failed
    check and at least one cancelled check copy named in ``cancelled_checks``,
    the head is refreshed once with update-branch (a stale cancelled copy holds
    the merge state; see ``ModelLandingPrFacts.cancelled_checks``).
    """

    CLEAN = "clean"
    BEHIND = "behind"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"


class EnumLandingSuspension(StrEnum):
    """An ineligibility that can lift with no head change (R2 ``suspended``)."""

    HOLD = "hold"
    GATE = "gate"
    DRAFT = "draft"
    DO_NOT_LAND = "do_not_land"


class EnumLandingResultKind(StrEnum):
    """What a worker's one result file may say.

    Only the first four are valid results (R1). The rest are recorded
    ``invalid`` and escalate.
    """

    MERGED = "merged"
    ARMED = "armed"
    FIX_SUBMITTED = "fix_submitted"
    EXTERNAL_BLOCKER = "external_blocker"
    WAITING_CI = "waiting_ci"
    WAITING_ORDER = "waiting_order"
    REPORT_ONLY = "report_only"
    BLOCKED = "blocked"
    UNPARSEABLE = "unparseable"


class EnumLandingOutcome(StrEnum):
    """A recorded outcome.

    The first six are the outcomes of a dispatch (P3: every dispatch reaches
    exactly one). ``blocked_on`` is recorded by the controller for a PR it did
    not dispatch because a parent is open (R3).
    """

    MERGED = "merged"
    ARMED = "armed"
    FIX_SUBMITTED = "fix_submitted"
    EXTERNAL_BLOCKER = "external_blocker"
    INVALID = "invalid"
    TIMED_OUT = "timed_out"
    BLOCKED_ON = "blocked_on"


BLOCKED_OUTCOMES: frozenset[EnumLandingOutcome] = frozenset(
    {EnumLandingOutcome.BLOCKED_ON, EnumLandingOutcome.EXTERNAL_BLOCKER}
)


class EnumLandingOutcomeReason(StrEnum):
    """Why an outcome is ``invalid`` or ``timed_out``; ``none`` otherwise."""

    NONE = "none"
    WAITING_CI = "waiting_ci"
    WAITING_ORDER = "waiting_order"
    REPORT_ONLY = "report_only"
    BARE_BLOCKED = "bare_blocked"
    UNPARSEABLE = "unparseable"
    HEAD_MISMATCH = "head_mismatch"
    UNAUTHORIZED_REWRITE = "unauthorized_rewrite"
    MISSING_EVIDENCE = "missing_evidence"
    UNVERIFIED_CLAIM = "unverified_claim"
    DEADLINE = "deadline"
    EXITED = "exited"
    REVOKED = "revoked"


class EnumLandingPushMode(StrEnum):
    """How a worker says it pushed. ``bare_force`` is never authorized."""

    FAST_FORWARD = "fast_forward"
    FORCE_WITH_LEASE = "force_with_lease"
    BARE_FORCE = "bare_force"


class EnumLandingRefUpdateKind(StrEnum):
    """GitHub's own record of a head ref update.

    ``force_push`` is a ``HeadRefForcePushedEvent``; ``fast_forward`` is a
    push whose new head descends from the old one.
    """

    FAST_FORWARD = "fast_forward"
    FORCE_PUSH = "force_push"


class EnumLandingExternalBlockerKind(StrEnum):
    """The named external blockers a worker may return (R1)."""

    OPERATOR_DECISION = "operator_decision"
    PRODUCTION_GATE = "production_gate"
    HOLD_ROW = "hold_row"
    COLLABORATOR = "collaborator"
    UPSTREAM_OPEN = "upstream_open"


class EnumLandingEngine(StrEnum):
    """Worker engines, in escalation order (R1 ladder)."""

    CLAUDE_SONNET = "claude_sonnet"
    CLAUDE_OPUS = "claude_opus"
    CODEX_HIGH = "codex_high"


CLAUDE_ENGINES: frozenset[EnumLandingEngine] = frozenset(
    {EnumLandingEngine.CLAUDE_SONNET, EnumLandingEngine.CLAUDE_OPUS}
)


class EnumLandingBriefClass(StrEnum):
    """The fixed worker brief classes, one brief per class."""

    REAL_RED = "real_red"
    CASCADE_RED = "cascade_red"
    BEHIND = "behind"
    CONFLICT_MECHANICAL = "conflict_mechanical"
    CONFLICT_SUBSTANTIVE = "conflict_substantive"
    COMPANION_RED = "companion_red"
    COMPANION_ORPHAN = "companion_orphan"
    RUNTIME = "runtime"


class EnumLandingActionKind(StrEnum):
    """The controller's actions (the handover mapping's left column).

    ``kill_worker`` and ``discard_result`` act on the controller's own
    workers; ``observe_only`` switches a drained repo to observe-only.
    """

    MERGE = "merge"
    RERUN = "rerun"
    UPDATE_BRANCH = "update_branch"
    ELIGIBILITY_RERUN = "eligibility_rerun"
    COMPANION_REBUILD = "companion_rebuild"
    COMPANION_CLOSE = "companion_close"
    DISPATCH_WORKER = "dispatch_worker"
    KILL_WORKER = "kill_worker"
    DISCARD_RESULT = "discard_result"
    OBSERVE_ONLY = "observe_only"


class EnumLandingRebuildStatus(StrEnum):
    """A rebuild request's record (R2). ``exhausted`` is ``rebuild_exhausted``."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXHAUSTED = "exhausted"


class EnumLandingMemberPosition(StrEnum):
    """A companion member's position (R2)."""

    CURRENT = "current"
    MOVED = "moved"
    GONE = "gone"


class EnumLandingMemberEligibility(StrEnum):
    """A companion member's eligibility (R2)."""

    ELIGIBLE = "eligible"
    SUSPENDED = "suspended"
    EXCLUDED = "excluded"


class EnumLandingCompanionVerdict(StrEnum):
    """Which R2 rule applied to an open companion this tick."""

    CLOSE = "close"
    WAIT = "wait"
    REBUILD = "rebuild"
    REBUILD_HELD_BY_LEASE = "rebuild_held_by_lease"
    REBUILD_ALREADY_REQUESTED = "rebuild_already_requested"
    MERGE = "merge"
    COMPANION_RED = "companion_red"
    CI_PENDING = "ci_pending"
    PENDING_REBUILD = "pending_rebuild"
    SUPERSEDED = "superseded"


class EnumLandingDegradedReason(StrEnum):
    """What sets DEGRADED.

    ``stale_refresh_exhausted``: a PR held at BLOCKED by stale cancelled check
    copies was refreshed ``max_stale_refreshes`` times and the class came back;
    no further refresh, no fix worker and no escalation (a person looks).
    """

    LEASE_STUCK = "lease_stuck"
    ESCALATION_EXHAUSTED = "escalation_exhausted"
    REBUILD_EXHAUSTED = "rebuild_exhausted"
    STALE_REFRESH_EXHAUSTED = "stale_refresh_exhausted"


__all__: list[str] = [
    "BLOCKED_OUTCOMES",
    "CLAUDE_ENGINES",
    "RERUNNABLE_RED_CLASSES",
    "EnumLandingActionKind",
    "EnumLandingBriefClass",
    "EnumLandingCi",
    "EnumLandingCompanionVerdict",
    "EnumLandingDegradedReason",
    "EnumLandingEngine",
    "EnumLandingExternalBlockerKind",
    "EnumLandingMemberEligibility",
    "EnumLandingMemberPosition",
    "EnumLandingMergeState",
    "EnumLandingOutcome",
    "EnumLandingOutcomeReason",
    "EnumLandingPrState",
    "EnumLandingPushMode",
    "EnumLandingRebuildStatus",
    "EnumLandingRedClass",
    "EnumLandingRefUpdateKind",
    "EnumLandingResultKind",
    "EnumLandingSuspension",
]
