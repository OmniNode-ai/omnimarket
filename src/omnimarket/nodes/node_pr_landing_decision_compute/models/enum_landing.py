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
    OWNED = "owned"


class EnumLandingGateReason(StrEnum):
    """Which gate a ``gate`` suspension stands for; a gate with none is refused.

    The collector's gate sources, one word each: ``companion_wait`` (red only on
    the change-control cascade while its companion is open), ``autobind`` (the
    producer is about to bind a cascade red), ``pending_required`` (a red head
    whose required contexts are still running), ``worker_gate`` (a judged
    external blocker or a spent head: no worker), ``base_blocked`` (a merge-queue
    ejection while the base is red), ``queued`` (in the merge queue),
    ``ledger_pause`` (a pause row names it), ``operator_main`` (based on main in
    an operator-gated repository), ``release_train`` (a bot's release PR, landed
    by release-cut), ``lab_unproven`` (a runtime head with no lab PASS readback),
    ``companion_subject`` (a change-control companion, never merged here) and
    ``retarget`` (based on a branch its repository does not land on).
    """

    COMPANION_WAIT = "companion_wait"
    AUTOBIND = "autobind"
    PENDING_REQUIRED = "pending_required"
    WORKER_GATE = "worker_gate"
    BASE_BLOCKED = "base_blocked"
    QUEUED = "queued"
    LEDGER_PAUSE = "ledger_pause"
    OPERATOR_MAIN = "operator_main"
    RELEASE_TRAIN = "release_train"
    LAB_UNPROVEN = "lab_unproven"
    COMPANION_SUBJECT = "companion_subject"
    RETARGET = "retarget"


# The gates whose premise is a red head: each holds a worker or a rerun, never a
# merge, so on a green, CLEAN head older than two ticks it holds nothing.
STALE_WHEN_GREEN_GATE_REASONS: frozenset[EnumLandingGateReason] = frozenset(
    {
        EnumLandingGateReason.COMPANION_WAIT,
        EnumLandingGateReason.AUTOBIND,
        EnumLandingGateReason.PENDING_REQUIRED,
        EnumLandingGateReason.WORKER_GATE,
    }
)


class EnumLandingLandSkipReason(StrEnum):
    """Why an open, green, CLEAN PR got no merge this tick (one per such PR).

    A suspension (``hold``, ``do_not_land``, ``draft``, ``owned``, ``gate`` with
    its reasons), a collaborator's PR, an open merge-order parent, a live lease
    whose verified head is not this one, a runtime PR inside an open companion,
    or a runtime PR while another holds the token.
    """

    HOLD = "hold"
    DO_NOT_LAND = "do_not_land"
    DRAFT = "draft"
    OWNED = "owned"
    GATE = "gate"
    COLLABORATOR = "collaborator"
    OPEN_PARENTS = "open_parents"
    LEASED = "leased"
    COMPANION_MEMBER = "companion_member"
    TOKEN_HELD = "token_held"


# A person's hold, a draft and do-not-land keep a PR out of every shared cause;
# ``owned`` (a lane's CLAIM on the PR) and a stalled ``gate`` do not.
CAUSE_EXCLUDED_SUSPENSIONS: frozenset[EnumLandingSuspension] = frozenset(
    {
        EnumLandingSuspension.HOLD,
        EnumLandingSuspension.DRAFT,
        EnumLandingSuspension.DO_NOT_LAND,
    }
)


class EnumLandingResultKind(StrEnum):
    """What a worker's one result file may say.

    Only the first four are valid results of a per-PR worker (R1). A cause
    worker's valid results are ``cause_fix_submitted``, ``cause_not_shared``
    and ``external_blocker``. The rest are recorded ``invalid`` and escalate.
    """

    MERGED = "merged"
    ARMED = "armed"
    FIX_SUBMITTED = "fix_submitted"
    EXTERNAL_BLOCKER = "external_blocker"
    CAUSE_FIX_SUBMITTED = "cause_fix_submitted"
    CAUSE_NOT_SHARED = "cause_not_shared"
    WAITING_CI = "waiting_ci"
    WAITING_ORDER = "waiting_order"
    REPORT_ONLY = "report_only"
    BLOCKED = "blocked"
    UNPARSEABLE = "unparseable"


class EnumLandingOutcome(StrEnum):
    """A recorded outcome.

    The first six are the outcomes of a dispatch (P3: every dispatch reaches
    exactly one). ``blocked_on`` is recorded by the controller for a PR it did
    not dispatch because a parent is open (R3). The two ``cause_`` outcomes
    are the verified results of a cause worker.
    """

    MERGED = "merged"
    ARMED = "armed"
    FIX_SUBMITTED = "fix_submitted"
    EXTERNAL_BLOCKER = "external_blocker"
    INVALID = "invalid"
    TIMED_OUT = "timed_out"
    BLOCKED_ON = "blocked_on"
    CAUSE_FIX_SUBMITTED = "cause_fix_submitted"
    CAUSE_NOT_SHARED = "cause_not_shared"


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
    SHARED_CAUSE = "shared_cause"


class EnumLandingActionKind(StrEnum):
    """The controller's actions (the handover mapping's left column).

    ``kill_worker`` and ``discard_result`` act on the controller's own
    workers; ``observe_only`` switches a drained repo to observe-only;
    ``escalate_operator`` asks the operator once about a parked cause.
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
    ESCALATE_OPERATOR = "escalate_operator"


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
    CAUSE_EXHAUSTED = "cause_exhausted"


__all__: list[str] = [
    "BLOCKED_OUTCOMES",
    "CAUSE_EXCLUDED_SUSPENSIONS",
    "CLAUDE_ENGINES",
    "RERUNNABLE_RED_CLASSES",
    "STALE_WHEN_GREEN_GATE_REASONS",
    "EnumLandingActionKind",
    "EnumLandingBriefClass",
    "EnumLandingCi",
    "EnumLandingCompanionVerdict",
    "EnumLandingDegradedReason",
    "EnumLandingEngine",
    "EnumLandingExternalBlockerKind",
    "EnumLandingGateReason",
    "EnumLandingLandSkipReason",
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
