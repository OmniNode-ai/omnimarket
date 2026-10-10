# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrLandingDecision: one tick of the landing controller, as a pure function.

Definition-B compute: ``handle(request: ModelLandingFacts) -> ModelLandingDecision``.
No clock (``observed_at`` is the snapshot's time), no I/O, no model, no envelope.
The same facts give byte-identical decisions in any input order.

The rules are the ``LandingController.tla`` model's controller actions (the
drain T3 model-before-build prerequisite), in the order one tick applies them:

1. The runtime token is freed first when its holder left (R6, ``TokenFree``).
2. Result files are read (``RecordResult``): a revoked lease id is discarded
   (S12); ``fix_submitted`` is verified against push evidence matched to
   GitHub's ref updates, never ancestry (R1, S16, S17); an invalid result is a
   violation and escalates.
3. Every lease is observed (``ObserveHead``), revoked when its repo drains with
   no result (``Revoke``), killed after its deadline, its post-result grace or
   its revocation (``Kill``), marked stuck when a kill did not take, and
   released only when the group probe and the tag scan are both empty
   (``ConfirmTerminated``, R7, P8). An unknown probe is alive.
4. A draining repo goes observe-only only with no lease in it (P7).
5. Rebuild records advance (R2): acknowledged, succeeded when a companion
   carrying the key exists in any state (finding LC-F1), failed, retried with
   the same key after the backoff, parked exhausted, redelivered when no
   delivery was recorded (S19, S20).
6. Companions, before any other selection (R2 rules 1 to 5), then uncovered
   members (``CoverRequest``).
7. Shared causes, before any per-PR dispatch: a park ends at its time or on
   a RELEASE row; a verified fix PR that merged gets one rerun per member head,
   and the members' next grade ends the attempt. Red PRs in one repo sharing a
   (check, failure signature) form a cluster (3 members, 2 under the landing
   floor), largest first, absorbing clusters mostly inside an earlier cause; a
   parked PR no cluster covers is a cause of one. Causes dispatch in floor,
   size and key order under the cause caps, inside the pool, with a two-attempt
   budget per park episode; a spent cause parks and escalates once.
8. Product PRs: chain roots only (R3), update-branch once per head, a head
   held at BLOCKED only by stale cancelled check copies refreshed once per
   head and at most ``max_stale_refreshes`` times per PR (then DEGRADED, no
   worker), blocked-outcome suppression by fingerprint (R8), one rerun per
   head (R5),
   the escalation ladder and parking (R1), the token (R6), pinned merge under a
   live lease (``PinMerge``), and dispatch in priority order (R4) under the
   worker pool, the per-repo cap, the load pause and the available engines
   (D5). No per-PR worker is dispatched for a member of a cause that holds a
   live lease or an open fix PR, or a park an owner's CLAIM covers; a park
   alone releases its members to per-PR dispatch (the fallback).

Gates are named: every gated open PR is listed with its ``gate_reasons``, and
every open, green, CLEAN PR left with no merge on its head carries one named
reason (a suspension, a collaborator, an open parent, a lease, a companion, the
token); a tick that leaves one with neither raises ``LandingCoverageError``. A
gate whose every reason's premise is a red head is released on a green, CLEAN
head once older than ``stale_gate_seconds``, so the head takes the merge path.

A fixer HOLD (a repo, or all) dispatches no worker in its scope and revokes
every live lease there through the kill sequence; merges, reruns and branch
updates go on.

Actions for an observe-only repo are returned in ``observed_actions`` and never
performed; no worker is dispatched in a draining repo.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from omnimarket.handlers.cause_signature import (
    CAUSE_PREFIX,
    UNREAD,
    cause_key,
    normalize_signature,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    BLOCKED_OUTCOMES,
    CAUSE_EXCLUDED_SUSPENSIONS,
    RERUNNABLE_RED_CLASSES,
    STALE_WHEN_GREEN_GATE_REASONS,
    EnumLandingActionKind,
    EnumLandingBriefClass,
    EnumLandingCi,
    EnumLandingCompanionVerdict,
    EnumLandingDegradedReason,
    EnumLandingEngine,
    EnumLandingLandSkipReason,
    EnumLandingMemberEligibility,
    EnumLandingMemberPosition,
    EnumLandingMergeState,
    EnumLandingOutcome,
    EnumLandingOutcomeReason,
    EnumLandingPrState,
    EnumLandingPushMode,
    EnumLandingRebuildStatus,
    EnumLandingRedClass,
    EnumLandingRefUpdateKind,
    EnumLandingResultKind,
    EnumLandingSuspension,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    BRIEF_INSTRUCTIONS,
    ModelLandingAction,
    ModelLandingCauseBrief,
    ModelLandingCompanionVerdictRow,
    ModelLandingDecision,
    ModelLandingDegraded,
    ModelLandingGateRow,
    ModelLandingLandSkip,
    ModelLandingRecordedOutcome,
    ModelLandingViolation,
    ModelLandingWorkerBrief,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    FIXER_HOLD_ALL,
    ModelLandingBlockerRef,
    ModelLandingCauseEscalation,
    ModelLandingCompanionFacts,
    ModelLandingFacts,
    ModelLandingPrFacts,
    ModelLandingRefUpdate,
    ModelLandingRerunRun,
    ModelLandingWorkerResult,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    ModelLandingCausePair,
    ModelLandingCauseRecord,
    ModelLandingControllerState,
    ModelLandingEligibilityRerun,
    ModelLandingLease,
    ModelLandingMemberRef,
    ModelLandingPrRecord,
    ModelLandingRebuildRecord,
    ModelLandingUncovered,
)

_INVALID_REASON: dict[EnumLandingResultKind, EnumLandingOutcomeReason] = {
    EnumLandingResultKind.WAITING_CI: EnumLandingOutcomeReason.WAITING_CI,
    EnumLandingResultKind.WAITING_ORDER: EnumLandingOutcomeReason.WAITING_ORDER,
    EnumLandingResultKind.REPORT_ONLY: EnumLandingOutcomeReason.REPORT_ONLY,
    EnumLandingResultKind.BLOCKED: EnumLandingOutcomeReason.BARE_BLOCKED,
    EnumLandingResultKind.UNPARSEABLE: EnumLandingOutcomeReason.UNPARSEABLE,
}
_CAUSE_RESULT_KINDS: frozenset[EnumLandingResultKind] = frozenset(
    {
        EnumLandingResultKind.CAUSE_FIX_SUBMITTED,
        EnumLandingResultKind.CAUSE_NOT_SHARED,
    }
)


def is_cause(subject: str) -> bool:
    """Whether a subject is a cause key rather than a PR."""
    return subject.startswith(CAUSE_PREFIX)


def repo_of(subject: str) -> str:
    """The ``owner/name`` of a ``repo#pr`` subject or a cause key (a repo is itself)."""
    if is_cause(subject):
        return subject[len(CAUSE_PREFIX) :].rsplit(":", 1)[0]
    return subject.split("#", 1)[0]


def rebuild_key(target_repo: str, members: tuple[ModelLandingMemberRef, ...]) -> str:
    """The idempotency key K: target repo plus the sorted membership fingerprint (R2)."""
    lines = sorted(f"{m.pr}@{m.head_sha}" for m in members)
    text = "\n".join([target_repo, *lines])
    return hashlib.sha256(text.encode()).hexdigest()


def blocker_fingerprint(refs: tuple[ModelLandingBlockerRef, ...]) -> str:
    """The hash of the sorted blocker refs, each with its state and head (R8)."""
    lines = sorted(f"{r.ref}|{r.state}|{r.head_sha or ''}" for r in refs)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


@dataclass(frozen=True)
class _Subject:
    """GitHub truth for whatever a worker was sent to: a PR or a companion."""

    head_sha: str
    state: EnumLandingPrState
    ref_updates: tuple[ModelLandingRefUpdate, ...]
    rerun_runs: tuple[ModelLandingRerunRun, ...]
    auto_merge_head: str | None


def _subject_of(
    pr: ModelLandingPrFacts | None, comp: ModelLandingCompanionFacts | None
) -> _Subject | None:
    if pr is not None:
        return _Subject(
            pr.head_sha, pr.state, pr.ref_updates, pr.rerun_runs, pr.auto_merge_head
        )
    if comp is not None:
        return _Subject(
            comp.head_sha, comp.state, comp.ref_updates, comp.rerun_runs, None
        )
    return None


@dataclass
class _Tick:
    """The mutable working copy of one tick; turned into the decision at the end."""

    facts: ModelLandingFacts
    now: datetime
    prs: dict[str, ModelLandingPrFacts]
    comps: dict[str, ModelLandingCompanionFacts]
    leases: dict[str, ModelLandingLease]
    records: dict[str, ModelLandingPrRecord]
    rebuilds: dict[str, ModelLandingRebuildRecord]
    uncovered: dict[str, ModelLandingUncovered]
    revoked: set[int]
    draining: set[str]
    observe: set[str]
    token: str | None
    next_lease_id: int
    close_requested: set[str]
    causes: dict[str, ModelLandingCauseRecord] = field(default_factory=dict)
    hold: set[str] = field(default_factory=set)
    formed: set[str] = field(default_factory=set)
    suppressed: set[str] = field(default_factory=set)
    elig_reruns: list[ModelLandingEligibilityRerun] = field(default_factory=list)
    actions: list[ModelLandingAction] = field(default_factory=list)
    observed: list[ModelLandingAction] = field(default_factory=list)
    recorded: list[ModelLandingRecordedOutcome] = field(default_factory=list)
    degraded: list[ModelLandingDegraded] = field(default_factory=list)
    violations: list[ModelLandingViolation] = field(default_factory=list)
    verdicts: list[ModelLandingCompanionVerdictRow] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    closing: set[str] = field(default_factory=set)
    close_next: set[str] = field(default_factory=set)
    suspensions: dict[str, tuple[EnumLandingSuspension, ...]] = field(
        default_factory=dict
    )
    released: set[str] = field(default_factory=set)
    skips: dict[str, EnumLandingLandSkipReason] = field(default_factory=dict)

    def emit(self, action: ModelLandingAction) -> None:
        if repo_of(action.subject) in self.observe:
            self.observed.append(action)
        else:
            self.actions.append(action)

    def record_of(self, pr: str) -> ModelLandingPrRecord:
        return self.records.get(pr) or ModelLandingPrRecord(pr=pr)

    def put_record(self, pr: str, **update: object) -> ModelLandingPrRecord:
        rec = self.record_of(pr).model_copy(update=update)
        self.records[pr] = rec
        return rec

    def subject(self, pr: str) -> _Subject | None:
        return _subject_of(self.prs.get(pr), self.comps.get(pr))

    def fingerprint(self, pr: str) -> str:
        facts = self.prs.get(pr)
        return blocker_fingerprint(facts.blocker_refs) if facts is not None else ""

    def held(self, repo: str) -> bool:
        """Under a fixer HOLD: no dispatch, and every live lease is revoked."""
        return FIXER_HOLD_ALL in self.hold or repo in self.hold


# ---------------------------------------------------------------- escalation
def _escalate(t: _Tick, pr: str) -> None:
    """One step up the R1 ladder; the third failure on one head parks the PR."""
    rec = t.record_of(pr)
    ladder = rec.ladder_index + 1
    update: dict[str, object] = {"ladder_index": ladder, "awaiting_head": None}
    subject = t.subject(pr)
    if ladder >= len(t.facts.policy.engine_ladder) and subject is not None:
        update["parked_head"] = subject.head_sha
        update["parked_fingerprint"] = t.fingerprint(pr)
    t.put_record(pr, **update)


def _record(
    t: _Tick,
    pr: str,
    lease_id: int | None,
    outcome: EnumLandingOutcome,
    reason: EnumLandingOutcomeReason = EnumLandingOutcomeReason.NONE,
) -> None:
    fp = t.fingerprint(pr)
    t.put_record(pr, outcome=outcome, reason=reason, blocker_fingerprint=fp)
    t.recorded.append(
        ModelLandingRecordedOutcome(
            pr=pr,
            lease_id=lease_id,
            outcome=outcome,
            reason=reason,
            blocker_fingerprint=fp,
        )
    )


# ------------------------------------------------------------------- results
def _verify_fix(
    lease: ModelLandingLease, result: ModelLandingWorkerResult, subj: _Subject
) -> EnumLandingOutcomeReason:
    """R1: a fix_submitted is verified against the authorized branch update.

    Returns ``none`` when it verifies, else the invalid reason.
    """
    head = result.head_sha
    if head is None or head != subj.head_sha:
        return EnumLandingOutcomeReason.HEAD_MISMATCH
    evidence = result.push_evidence
    if not evidence:
        if result.rerun_run_id is not None:
            ok = any(
                run.run_id == result.rerun_run_id
                and run.head_sha == head
                and run.created_at > lease.dispatched_at
                for run in subj.rerun_runs
            )
            # a rerun names a run created after dispatch on the unchanged head
            if ok and head == lease.dispatch_head:
                return EnumLandingOutcomeReason.NONE
            return EnumLandingOutcomeReason.MISSING_EVIDENCE
        if head == lease.dispatch_head:
            return EnumLandingOutcomeReason.MISSING_EVIDENCE
        return EnumLandingOutcomeReason.UNAUTHORIZED_REWRITE
    bad = EnumLandingOutcomeReason.UNAUTHORIZED_REWRITE
    if evidence[0].expected_old_head not in lease.seen_heads:
        return bad
    for index, entry in enumerate(evidence):
        if (
            entry.mode is EnumLandingPushMode.BARE_FORCE
            or entry.expected_old_head is None
        ):
            return bad
        if (
            index + 1 < len(evidence)
            and evidence[index + 1].expected_old_head != entry.new_head
        ):
            return bad
        want = (
            EnumLandingRefUpdateKind.FORCE_PUSH
            if entry.mode is EnumLandingPushMode.FORCE_WITH_LEASE
            else EnumLandingRefUpdateKind.FAST_FORWARD
        )
        matched = any(
            update.before_sha == entry.expected_old_head
            and update.after_sha == entry.new_head
            and update.kind is want
            and update.at > lease.dispatched_at
            for update in subj.ref_updates
        )
        if not matched:
            return bad
    if evidence[-1].new_head != head:
        return bad
    return EnumLandingOutcomeReason.NONE


def _read_result(t: _Tick, result: ModelLandingWorkerResult) -> None:
    lease = next(
        (le for le in t.leases.values() if le.lease_id == result.lease_id), None
    )
    discard = ModelLandingAction(
        kind=EnumLandingActionKind.DISCARD_RESULT,
        subject=result.pr,
        lease_id=result.lease_id,
    )
    if (
        result.lease_id in t.revoked
        or lease is None
        or lease.revoked
        or lease.result_recorded_at is not None
        or lease.pr != result.pr
    ):
        t.emit(discard)
        return
    if is_cause(lease.pr):
        _read_cause_result(t, lease, result)
        return
    pr = lease.pr
    subj = t.subject(pr)
    kind = result.kind
    reason = EnumLandingOutcomeReason.NONE
    outcome = EnumLandingOutcome.INVALID
    if kind in _INVALID_REASON:
        reason = _INVALID_REASON[kind]
    elif subj is None:
        reason = EnumLandingOutcomeReason.UNVERIFIED_CLAIM
    elif kind is EnumLandingResultKind.MERGED:
        if subj.state is EnumLandingPrState.MERGED:
            outcome = EnumLandingOutcome.MERGED
        else:
            reason = EnumLandingOutcomeReason.UNVERIFIED_CLAIM
    elif kind is EnumLandingResultKind.ARMED:
        if (
            subj.state is EnumLandingPrState.OPEN
            and subj.auto_merge_head == subj.head_sha
        ):
            outcome = EnumLandingOutcome.ARMED
        else:
            reason = EnumLandingOutcomeReason.UNVERIFIED_CLAIM
    elif kind is EnumLandingResultKind.EXTERNAL_BLOCKER:
        if result.blocker_kind is not None and result.blocker_ref:
            outcome = EnumLandingOutcome.EXTERNAL_BLOCKER
        else:
            reason = EnumLandingOutcomeReason.BARE_BLOCKED
    elif kind in _CAUSE_RESULT_KINDS:  # a cause result from a per-PR worker
        reason = EnumLandingOutcomeReason.UNVERIFIED_CLAIM
    else:  # fix_submitted
        reason = _verify_fix(lease, result, subj)
        if reason is EnumLandingOutcomeReason.NONE:
            outcome = EnumLandingOutcome.FIX_SUBMITTED
    t.leases[pr] = lease.model_copy(update={"result_recorded_at": t.now})
    _record(t, pr, lease.lease_id, outcome, reason)
    if outcome is EnumLandingOutcome.FIX_SUBMITTED:
        t.put_record(
            pr,
            awaiting_head=result.head_sha,
            attempt_red_checks=lease.dispatch_red_checks,
        )
    elif outcome is EnumLandingOutcome.INVALID:
        t.violations.append(
            ModelLandingViolation(pr=pr, lease_id=lease.lease_id, reason=reason)
        )
        _escalate(t, pr)


# -------------------------------------------------------------------- leases
def _kill(t: _Tick, lease: ModelLandingLease) -> ModelLandingLease:
    """Run the kill sequence; a kill already sent that did not take is stuck (R7)."""
    t.emit(
        ModelLandingAction(
            kind=EnumLandingActionKind.KILL_WORKER,
            subject=lease.pr,
            lease_id=lease.lease_id,
        )
    )
    if lease.kill_sent_tick is None:
        return lease.model_copy(update={"kill_sent_tick": t.facts.tick})
    t.degraded.append(
        ModelLandingDegraded(
            reason=EnumLandingDegradedReason.LEASE_STUCK,
            subject=f"{lease.pr} lease {lease.lease_id}",
        )
    )
    return lease.model_copy(update={"stuck": True})


def _observe_lease(t: _Tick, lease: ModelLandingLease) -> None:
    pr = lease.pr
    subj = t.subject(pr)
    if subj is not None and subj.head_sha != lease.last_seen_head:
        heads = lease.seen_heads
        if subj.head_sha not in heads:
            heads = (*heads, subj.head_sha)
        lease = lease.model_copy(
            update={"last_seen_head": subj.head_sha, "seen_heads": heads}
        )
    probe = next((p for p in t.facts.probes if p.lease_id == lease.lease_id), None)
    alive = probe is None or probe.group_alive or probe.tagged_alive
    if not alive:
        del t.leases[pr]
        if is_cause(pr):
            _cause_lease_ended(t, lease)
            return
        if lease.result_recorded_at is None:
            if lease.revoked:
                why = EnumLandingOutcomeReason.REVOKED
            elif t.now >= lease.deadline_at:
                why = EnumLandingOutcomeReason.DEADLINE
            else:
                why = EnumLandingOutcomeReason.EXITED
            _record(t, pr, lease.lease_id, EnumLandingOutcome.TIMED_OUT, why)
            if not lease.revoked:
                _escalate(t, pr)
        return
    if (
        (repo_of(pr) in t.draining or t.held(repo_of(pr)))
        and lease.result_recorded_at is None
        and not lease.revoked
    ):
        lease = lease.model_copy(update={"revoked": True})
        t.revoked.add(lease.lease_id)
    grace = timedelta(seconds=t.facts.policy.exit_grace_seconds)
    due = (
        lease.revoked
        or t.now >= lease.deadline_at
        or (
            lease.result_recorded_at is not None
            and t.now >= lease.result_recorded_at + grace
        )
    )
    if due:
        lease = _kill(t, lease)
    t.leases[pr] = lease


def _drain(t: _Tick) -> None:
    """P7: observe-only only for a draining repo with no lease left in it."""
    for repo in sorted(t.draining - t.observe):
        if any(repo_of(pr) == repo for pr in t.leases):
            t.refused.append(repo)
            continue
        t.emit(
            ModelLandingAction(kind=EnumLandingActionKind.OBSERVE_ONLY, subject=repo)
        )
        t.observe.add(repo)


# ------------------------------------------------------------------ rebuilds
def _deliver(t: _Tick, rec: ModelLandingRebuildRecord) -> None:
    t.emit(
        ModelLandingAction(
            kind=EnumLandingActionKind.COMPANION_REBUILD,
            subject=rec.old_companion or rec.target_repo,
            rebuild_key=rec.key,
            target_repo=rec.target_repo,
            members=rec.members,
        )
    )


def _exhausted(t: _Tick, rec: ModelLandingRebuildRecord) -> None:
    t.degraded.append(
        ModelLandingDegraded(
            reason=EnumLandingDegradedReason.REBUILD_EXHAUSTED,
            subject=f"{rec.old_companion or rec.target_repo} {rec.key}",
        )
    )


def _fail(t: _Tick, rec: ModelLandingRebuildRecord) -> ModelLandingRebuildRecord:
    policy = t.facts.policy
    if rec.attempts >= policy.max_rebuild_attempts:
        rec = rec.model_copy(update={"status": EnumLandingRebuildStatus.EXHAUSTED})
        _exhausted(t, rec)
        return rec
    backoff = policy.rebuild_backoff_ticks
    wait = backoff[min(rec.attempts - 1, len(backoff) - 1)]
    return rec.model_copy(
        update={
            "status": EnumLandingRebuildStatus.FAILED,
            "next_retry_tick": t.facts.tick + wait,
        }
    )


def _rearm(
    t: _Tick, rec: ModelLandingRebuildRecord, **update: object
) -> ModelLandingRebuildRecord:
    rec = rec.model_copy(
        update={
            "status": EnumLandingRebuildStatus.PENDING,
            "delivered_tick": None,
            "run_id": None,
            "next_retry_tick": None,
            **update,
        }
    )
    _deliver(t, rec)
    return rec


def _advance_rebuilds(t: _Tick) -> None:
    """R2: pending, succeeded, failed, retried, parked; written ahead of delivery."""
    tick = t.facts.tick
    acks = {a.key: a.run_id for a in t.facts.rebuild_acks}
    failures = set(t.facts.producer_failures)
    carriers = {c.rebuild_key: c.pr for c in t.comps.values() if c.rebuild_key}
    for key in sorted(t.rebuilds):
        rec = t.rebuilds[key]
        status = rec.status
        if status is EnumLandingRebuildStatus.PENDING:
            if key in acks and rec.delivered_tick is None:
                rec = rec.model_copy(
                    update={"delivered_tick": tick, "run_id": acks[key]}
                )
            if key in carriers:  # any state: model finding LC-F1
                rec = rec.model_copy(
                    update={
                        "status": EnumLandingRebuildStatus.SUCCEEDED,
                        "replacement": carriers[key],
                    }
                )
            elif key in failures:
                rec = _fail(t, rec)
            elif rec.delivered_tick is None:
                _deliver(t, rec)  # a crash lost the delivery: redeliver K (S19, S20)
            elif tick - rec.delivered_tick >= t.facts.policy.rebuild_visibility_ticks:
                rec = _fail(t, rec)
        elif status is EnumLandingRebuildStatus.FAILED:
            if rec.next_retry_tick is not None and tick >= rec.next_retry_tick:
                rec = _rearm(t, rec, attempts=rec.attempts + 1)
        elif status is EnumLandingRebuildStatus.EXHAUSTED:
            if t.facts.producer_release != rec.producer_release:
                rec = _rearm(
                    t, rec, attempts=1, producer_release=t.facts.producer_release
                )
            else:
                _exhausted(t, rec)
        t.rebuilds[key] = rec


def _eligibility_reruns(t: _Tick) -> None:
    """S6: each member of a companion the controller merged is rerun whole once."""
    for owed in sorted(
        t.facts.state.eligibility_reruns, key=lambda e: (e.pr, e.companion)
    ):
        comp = t.comps.get(owed.companion)
        member = t.prs.get(owed.pr)
        if (
            comp is not None
            and comp.state is EnumLandingPrState.MERGED
            and member is not None
            and member.state is EnumLandingPrState.OPEN
        ):
            t.emit(
                ModelLandingAction(
                    kind=EnumLandingActionKind.ELIGIBILITY_RERUN, subject=owed.pr
                )
            )


# ------------------------------------------------------------------ dispatch
def _try_dispatch(
    t: _Tick,
    subject: str,
    brief_class: EnumLandingBriefClass,
    head: str,
    red_checks: tuple[str, ...],
) -> bool:
    """Dispatch one worker, when the pool, the load and the engines allow (D5, P7)."""
    policy = t.facts.policy
    repo = repo_of(subject)
    if repo in t.draining or t.held(repo) or subject in t.leases:
        return False
    if t.facts.load1 > policy.load_pause_threshold:
        return False
    if len(t.leases) >= policy.max_workers:
        return False
    if sum(1 for s in t.leases if repo_of(s) == repo) >= policy.max_workers_per_repo:
        return False
    rec = t.record_of(subject)
    available = set(t.facts.available_engines)
    engine: EnumLandingEngine | None = next(
        (e for e in policy.engine_ladder[rec.ladder_index :] if e in available), None
    )
    if engine is None:
        return False
    lease_id = t.next_lease_id
    t.next_lease_id += 1
    deadline = t.now + timedelta(seconds=policy.lease_seconds)
    t.leases[subject] = ModelLandingLease(
        pr=subject,
        lease_id=lease_id,
        brief_class=brief_class,
        engine=engine,
        dispatched_at=t.now,
        deadline_at=deadline,
        dispatch_head=head,
        seen_heads=(head,),
        last_seen_head=head,
        dispatch_red_checks=tuple(sorted(red_checks)),
    )
    t.put_record(subject, awaiting_head=None)
    t.emit(
        ModelLandingAction(
            kind=EnumLandingActionKind.DISPATCH_WORKER,
            subject=subject,
            head_sha=head,
            lease_id=lease_id,
            brief=ModelLandingWorkerBrief(
                brief_class=brief_class,
                pr=subject,
                head_sha=head,
                lease_id=lease_id,
                engine=engine,
                deadline_at=deadline,
                instructions=BRIEF_INSTRUCTIONS[brief_class],
            ),
        )
    )
    return True


# ---------------------------------------------------------------- companions
def _classify(
    t: _Tick, member: ModelLandingMemberRef
) -> tuple[EnumLandingMemberPosition, EnumLandingMemberEligibility] | None:
    facts = t.prs.get(member.pr)
    if facts is None:
        return None  # unknown: fail closed
    if facts.state is not EnumLandingPrState.OPEN:
        return EnumLandingMemberPosition.GONE, EnumLandingMemberEligibility.EXCLUDED
    position = (
        EnumLandingMemberPosition.CURRENT
        if facts.head_sha == member.head_sha
        else EnumLandingMemberPosition.MOVED
    )
    if facts.collaborator:
        return position, EnumLandingMemberEligibility.EXCLUDED
    if facts.suspensions:
        return position, EnumLandingMemberEligibility.SUSPENDED
    return position, EnumLandingMemberEligibility.ELIGIBLE


def _verdict(
    t: _Tick, comp: ModelLandingCompanionFacts, verdict: EnumLandingCompanionVerdict
) -> None:
    t.verdicts.append(
        ModelLandingCompanionVerdictRow(companion=comp.pr, verdict=verdict)
    )


def _close(t: _Tick, comp: ModelLandingCompanionFacts, replacement: str | None) -> None:
    t.closing.add(comp.pr)
    if comp.pr in t.close_requested:
        # closed last tick and still open: a worker closes it (companion_orphan)
        _try_dispatch(
            t, comp.pr, EnumLandingBriefClass.COMPANION_ORPHAN, comp.head_sha, ()
        )
        t.close_next.add(comp.pr)
        return
    t.emit(
        ModelLandingAction(
            kind=EnumLandingActionKind.COMPANION_CLOSE,
            subject=comp.pr,
            replacement=replacement,
        )
    )
    t.close_next.add(comp.pr)


def _request(
    t: _Tick,
    target_repo: str,
    members: tuple[ModelLandingMemberRef, ...],
    old: str | None,
) -> None:
    key = rebuild_key(target_repo, members)
    rec = ModelLandingRebuildRecord(
        key=key,
        target_repo=target_repo,
        members=members,
        old_companion=old,
        status=EnumLandingRebuildStatus.PENDING,
        attempts=1,
        producer_release=t.facts.producer_release,
    )
    t.rebuilds[key] = rec
    _deliver(t, rec)


def _companion(t: _Tick, comp: ModelLandingCompanionFacts) -> None:
    """R2: rules 1 to 5, after supersession and pending requests."""
    V = EnumLandingCompanionVerdict  # noqa: N806 - short alias for the verdicts
    classes = [(m, _classify(t, m)) for m in sorted(comp.members, key=lambda m: m.pr)]
    known = [(m, c) for m, c in classes if c is not None]
    if len(known) != len(classes):
        _verdict(t, comp, V.WAIT)
        return
    own = sorted(
        (r for r in t.rebuilds.values() if r.old_companion == comp.pr),
        key=lambda r: r.key,
    )
    pending = any(r.status is EnumLandingRebuildStatus.PENDING for r in own)
    done = [r for r in own if r.status is EnumLandingRebuildStatus.SUCCEEDED]
    if done and not pending:
        _verdict(t, comp, V.SUPERSEDED)
        _close(t, comp, done[0].replacement)
        return
    eligible = [m for m, c in known if c[1] is EnumLandingMemberEligibility.ELIGIBLE]
    suspended = [m for m, c in known if c[1] is EnumLandingMemberEligibility.SUSPENDED]
    ready = all(
        c == (EnumLandingMemberPosition.CURRENT, EnumLandingMemberEligibility.ELIGIBLE)
        for _, c in known
    )
    if not eligible:
        if pending:
            _verdict(t, comp, V.PENDING_REBUILD)
        elif suspended:
            _verdict(t, comp, V.WAIT)
        else:
            _verdict(t, comp, V.CLOSE)
            _close(t, comp, None)
        return
    if not ready:
        members = tuple(
            ModelLandingMemberRef(pr=m.pr, head_sha=t.prs[m.pr].head_sha)
            for m in eligible
        )
        key = rebuild_key(repo_of(comp.pr), members)
        if key in t.rebuilds:
            status = t.rebuilds[key].status
            _verdict(
                t,
                comp,
                V.PENDING_REBUILD
                if status is EnumLandingRebuildStatus.PENDING
                else V.REBUILD_ALREADY_REQUESTED,
            )
        elif any(m.pr in t.leases for m in members):
            _verdict(t, comp, V.REBUILD_HELD_BY_LEASE)
        else:
            _request(t, repo_of(comp.pr), members, comp.pr)
            for m in suspended:
                t.uncovered.setdefault(
                    m.pr, ModelLandingUncovered(pr=m.pr, target_repo=repo_of(comp.pr))
                )
            _verdict(t, comp, V.REBUILD)
        return
    if pending:
        _verdict(t, comp, V.PENDING_REBUILD)
    elif comp.ci is EnumLandingCi.GREEN:
        _verdict(t, comp, V.MERGE)
        t.emit(
            ModelLandingAction(
                kind=EnumLandingActionKind.MERGE,
                subject=comp.pr,
                head_sha=comp.head_sha,
            )
        )
        t.elig_reruns.extend(
            ModelLandingEligibilityRerun(pr=m.pr, companion=comp.pr) for m, _ in known
        )
    elif comp.ci is EnumLandingCi.RED:
        _verdict(t, comp, V.COMPANION_RED)
        _try_dispatch(
            t, comp.pr, EnumLandingBriefClass.COMPANION_RED, comp.head_sha, ()
        )
    else:
        _verdict(t, comp, V.CI_PENDING)


def _cover(t: _Tick) -> None:
    """An uncovered member that is eligible again gets its own request (LCover)."""
    for pr in sorted(t.uncovered):
        unc = t.uncovered[pr]
        facts = t.prs.get(pr)
        if facts is None or facts.state is not EnumLandingPrState.OPEN:
            del t.uncovered[pr]
            continue
        if facts.suspensions or facts.collaborator or pr in t.leases:
            continue
        del t.uncovered[pr]
        members = (ModelLandingMemberRef(pr=pr, head_sha=facts.head_sha),)
        carried = any(
            c.state is EnumLandingPrState.OPEN
            and c.pr not in t.closing
            and any(m.pr == pr and m.head_sha == facts.head_sha for m in c.members)
            for c in t.comps.values()
        )
        if carried or rebuild_key(unc.target_repo, members) in t.rebuilds:
            continue
        _request(t, unc.target_repo, members, None)


# ------------------------------------------------------------------- causes
def _cause_lease_ended(t: _Tick, lease: ModelLandingLease) -> None:
    """A cause lease confirmed terminated with no result.

    The deadline passing with the process alive spends an attempt, and so does
    an exit with no result at or after the spawn window (LC-F2: counted
    nowhere, a cause whose worker keeps dying was redispatched without bound).
    Only an exit inside the window (a usage limit, a spawn failure) counts
    toward the spawn-failure budget instead; a revoke spends nothing.
    """
    if lease.result_recorded_at is not None:
        return
    policy = t.facts.policy
    if lease.revoked:
        why = EnumLandingOutcomeReason.REVOKED
    elif t.now >= lease.deadline_at:
        why = EnumLandingOutcomeReason.DEADLINE
    else:
        why = EnumLandingOutcomeReason.EXITED
    t.recorded.append(
        ModelLandingRecordedOutcome(
            pr=lease.pr,
            lease_id=lease.lease_id,
            outcome=EnumLandingOutcome.TIMED_OUT,
            reason=why,
        )
    )
    rec = t.causes.get(lease.pr)
    if rec is None:
        return
    update: dict[str, object] = {"outcome": EnumLandingOutcome.TIMED_OUT, "reason": why}
    window = timedelta(seconds=policy.cause_spawn_failure_seconds)
    if why is EnumLandingOutcomeReason.EXITED and t.now - lease.dispatched_at < window:
        update["spawn_failures"] = rec.spawn_failures + 1
    elif why is not EnumLandingOutcomeReason.REVOKED:
        update["attempts"] = rec.attempts + 1
    t.causes[lease.pr] = rec.model_copy(update=update)


def _read_cause_result(
    t: _Tick, lease: ModelLandingLease, result: ModelLandingWorkerResult
) -> None:
    """A cause worker's one result: recorded, verified, and one attempt spent."""
    key = lease.pr
    kind = result.kind
    outcome = EnumLandingOutcome.INVALID
    reason = EnumLandingOutcomeReason.NONE
    if kind is EnumLandingResultKind.CAUSE_FIX_SUBMITTED:
        fix = t.prs.get(result.fix_ref) if result.fix_ref else None
        verified = (
            fix is not None
            and result.fix_head is not None
            and (
                fix.state is EnumLandingPrState.MERGED
                or (
                    fix.state is EnumLandingPrState.OPEN
                    and fix.head_sha == result.fix_head
                )
            )
        )
        if verified:
            outcome = EnumLandingOutcome.CAUSE_FIX_SUBMITTED
        else:
            reason = EnumLandingOutcomeReason.UNVERIFIED_CLAIM
    elif kind is EnumLandingResultKind.CAUSE_NOT_SHARED:
        outcome = EnumLandingOutcome.CAUSE_NOT_SHARED
    elif kind is EnumLandingResultKind.EXTERNAL_BLOCKER:
        if result.blocker_kind is not None and result.blocker_ref:
            outcome = EnumLandingOutcome.EXTERNAL_BLOCKER
        else:
            reason = EnumLandingOutcomeReason.BARE_BLOCKED
    elif kind in _INVALID_REASON:
        reason = _INVALID_REASON[kind]
    else:  # a per-PR result from a cause worker
        reason = EnumLandingOutcomeReason.UNVERIFIED_CLAIM
    t.leases[key] = lease.model_copy(update={"result_recorded_at": t.now})
    t.recorded.append(
        ModelLandingRecordedOutcome(
            pr=key, lease_id=lease.lease_id, outcome=outcome, reason=reason
        )
    )
    if outcome is EnumLandingOutcome.INVALID:
        t.violations.append(
            ModelLandingViolation(pr=key, lease_id=lease.lease_id, reason=reason)
        )
    rec = t.causes.get(key)
    if rec is None:
        return
    update: dict[str, object] = {
        "attempts": rec.attempts + 1,
        "outcome": outcome,
        "reason": reason,
    }
    if outcome is EnumLandingOutcome.CAUSE_FIX_SUBMITTED:
        update.update(fix_ref=result.fix_ref, fix_head=result.fix_head, rerun_at=None)
    elif outcome is EnumLandingOutcome.CAUSE_NOT_SHARED:
        # back to the per-PR path at rung 0; the key stays blocked for this set
        current = tuple(
            ModelLandingMemberRef(pr=m.pr, head_sha=t.prs[m.pr].head_sha)
            for m in rec.members
            if m.pr in t.prs
        )
        if current:
            update["members"] = current
        for m in current:
            t.put_record(
                m.pr,
                ladder_index=0,
                parked_head=None,
                parked_fingerprint="",
                awaiting_head=None,
            )
    t.causes[key] = rec.model_copy(update=update)


def _parked(t: _Tick, rec: ModelLandingCauseRecord) -> bool:
    return rec.parked_until is not None and t.now < rec.parked_until


def _leased_or_fixing(t: _Tick, rec: ModelLandingCauseRecord) -> bool:
    leased = (
        rec.key in t.leases and rec.outcome is not EnumLandingOutcome.CAUSE_NOT_SHARED
    )
    fixing = rec.outcome is EnumLandingOutcome.CAUSE_FIX_SUBMITTED
    return leased or fixing


def _active(t: _Tick, rec: ModelLandingCauseRecord) -> bool:
    """A live cause: a live lease, a fix PR in flight, or a park."""
    return _leased_or_fixing(t, rec) or _parked(t, rec)


def _holds_members(t: _Tick, rec: ModelLandingCauseRecord) -> bool:
    """A cause holding its members off per-PR dispatch.

    A live lease, a fix PR in flight, or a park that an owner's CLAIM still
    covers. A park alone does not: its members take the per-PR path.
    """
    return _leased_or_fixing(t, rec) or (_parked(t, rec) and _owned(t, rec))


def _blocks(t: _Tick, rec: ModelLandingCauseRecord) -> bool:
    """cause_not_shared blocks its key until a member's head moves or it leaves."""
    if rec.outcome is not EnumLandingOutcome.CAUSE_NOT_SHARED:
        return False
    for m in rec.members:
        p = t.prs.get(m.pr)
        if p is None or p.state is not EnumLandingPrState.OPEN:
            return False
        if p.head_sha != m.head_sha:
            return False
    return True


def _owned(t: _Tick, rec: ModelLandingCauseRecord) -> bool:
    """Owned by a live CLAIM naming the cause (by key, or by its repo and a check)."""
    checks = {pair.check for pair in rec.pairs}
    return any(
        owner.cause == rec.key
        or (owner.cause is None and owner.repo == rec.repo and owner.check in checks)
        for owner in t.facts.cause_owners
    )


def _keep_cause(t: _Tick, rec: ModelLandingCauseRecord) -> bool:
    return (
        rec.key in t.formed or rec.key in t.leases or _active(t, rec) or _blocks(t, rec)
    )


def _regraded(t: _Tick, rec: ModelLandingCauseRecord) -> bool:
    """Every member rerun after the fix merged has graded again (or moved on)."""
    assert rec.rerun_at is not None
    for m in rec.members:
        p = t.prs.get(m.pr)
        if p is None or p.state is not EnumLandingPrState.OPEN:
            continue
        if ModelLandingMemberRef(pr=m.pr, head_sha=p.head_sha) not in rec.rerun_heads:
            continue  # its head moved: its own change, graded with the clusters
        if p.ci is EnumLandingCi.PENDING:
            return False
        if not any(
            run.head_sha == p.head_sha and run.created_at >= rec.rerun_at
            for run in p.rerun_runs
        ):
            return False
    return True


def _advance_causes(t: _Tick) -> None:
    """Parks end; a merged fix reruns each member head once; a re-grade ends the attempt."""
    park = timedelta(hours=t.facts.policy.cause_park_hours)
    for key in sorted(t.causes):
        rec = t.causes[key]
        if rec.parked_until is not None:
            start = rec.parked_until - park
            released = any(
                r.cause == key and r.at >= start for r in t.facts.cause_releases
            )
            if t.now >= rec.parked_until or released:
                rec = rec.model_copy(
                    update={
                        "parked_until": None,
                        "attempts": 0,
                        "spawn_failures": 0,
                        "outcome": None,
                        "reason": EnumLandingOutcomeReason.NONE,
                    }
                )
            else:
                t.degraded.append(
                    ModelLandingDegraded(
                        reason=EnumLandingDegradedReason.CAUSE_EXHAUSTED, subject=key
                    )
                )
        if rec.outcome is EnumLandingOutcome.CAUSE_FIX_SUBMITTED and rec.fix_ref:
            fix = t.prs.get(rec.fix_ref)
            ended = False
            if fix is None or fix.state is EnumLandingPrState.OPEN:
                pass  # members wait on the fix PR (an unread fix PR fails closed)
            elif fix.state is EnumLandingPrState.CLOSED:
                ended = True  # closed unmerged: the attempt failed
            elif rec.rerun_at is None:
                heads: list[ModelLandingMemberRef] = []
                for m in sorted(rec.members, key=lambda m: m.pr):
                    p = t.prs.get(m.pr)
                    if p is None or p.state is not EnumLandingPrState.OPEN:
                        continue
                    ref = ModelLandingMemberRef(pr=m.pr, head_sha=p.head_sha)
                    if ref in rec.rerun_heads:
                        continue
                    heads.append(ref)
                    t.emit(
                        ModelLandingAction(
                            kind=EnumLandingActionKind.RERUN,
                            subject=m.pr,
                            head_sha=p.head_sha,
                        )
                    )
                rec = rec.model_copy(
                    update={
                        "rerun_at": t.now,
                        "rerun_heads": (*rec.rerun_heads, *heads),
                    }
                )
            else:
                ended = _regraded(t, rec)
            if ended:
                rec = rec.model_copy(
                    update={
                        "outcome": None,
                        "reason": EnumLandingOutcomeReason.NONE,
                        "rerun_at": None,
                    }
                )
        t.causes[key] = rec


def _cause_candidate(t: _Tick, p: ModelLandingPrFacts) -> bool:
    """A red PR that may join a cluster.

    A person's hold, a draft and do-not-land never join; a lane's CLAIM
    (``owned``) does; a ``gate`` joins once it is older than the stall window.
    A head whose one rerun is still unused reruns first.
    """
    if (
        p.state is not EnumLandingPrState.OPEN
        or p.ci is not EnumLandingCi.RED
        or p.collaborator
    ):
        return False
    if any(s in CAUSE_EXCLUDED_SUSPENSIONS for s in p.suspensions):
        return False
    if EnumLandingSuspension.GATE in p.suspensions:
        stall = timedelta(minutes=t.facts.policy.wait_stall_minutes)
        if p.gate_since is None or t.now - p.gate_since <= stall:
            return False
    return not (
        p.red_class in RERUNNABLE_RED_CLASSES
        and p.head_sha not in t.record_of(p.pr).rerun_heads
    )


def _pairs_of(t: _Tick, p: ModelLandingPrFacts) -> list[tuple[str, str, str]]:
    """The (check, signature, annotation) of each readable, clusterable red check."""
    excluded = set(t.facts.policy.cluster_excluded_checks)
    out = []
    for check in sorted(set(p.red_checks)):
        if check in excluded:
            continue
        text = p.red_annotations.get(check)
        signature = normalize_signature(check, text)
        if signature != UNREAD and text is not None:
            out.append((check, signature, text))
    return out


@dataclass
class _Cause:
    key: str
    repo: str
    pairs: list[ModelLandingCausePair]
    members: set[str]


def _parked_pr(t: _Tick, p: ModelLandingPrFacts) -> bool:
    rec = t.record_of(p.pr)
    return (
        rec.parked_head is not None
        and rec.parked_head == p.head_sha
        and rec.parked_fingerprint == t.fingerprint(p.pr)
    )


def _form_causes(t: _Tick) -> None:
    """Clusters, largest first with absorption, then escalated singletons; upserted by key."""
    policy = t.facts.policy
    floor = set(t.facts.floor_breached_repos)
    held_members = {
        m.pr for rec in t.causes.values() if _active(t, rec) for m in rec.members
    }
    groups: dict[tuple[str, str, str], set[str]] = {}
    examples: dict[tuple[str, str, str], str] = {}
    for p in sorted(t.prs.values(), key=lambda p: p.pr):
        if p.pr in held_members or not _cause_candidate(t, p):
            continue
        for check, signature, text in _pairs_of(t, p):
            group = (repo_of(p.pr), check, signature)
            groups.setdefault(group, set()).add(p.pr)
            examples.setdefault(group, text)

    def need(repo: str) -> int:
        if repo in floor:
            return policy.cluster_min_members_floor
        return policy.cluster_min_members

    clusters = sorted(
        ((g, frozenset(m)) for g, m in groups.items() if len(m) >= need(g[0])),
        key=lambda c: (-len(c[1]), c[0]),
    )
    chosen: list[_Cause] = []
    taken: dict[str, str] = {}
    for (repo, check, signature), members in clusters:
        pair = ModelLandingCausePair(
            check=check,
            signature=signature,
            example=examples[(repo, check, signature)],
            members=len(members),
        )
        host = next(
            (
                c
                for c in chosen
                if c.repo == repo
                and len(members & c.members)
                >= policy.cluster_absorb_ratio * len(members)
            ),
            None,
        )
        if host is not None:
            host.pairs.append(pair)
            for pr in sorted(members - taken.keys()):
                host.members.add(pr)
                taken[pr] = host.key
            continue
        free = set(members - taken.keys())
        if len(free) < need(repo):
            continue
        cause = _Cause(cause_key(repo, signature), repo, [pair], free)
        chosen.append(cause)
        taken.update(dict.fromkeys(free, cause.key))
    for p in sorted(t.prs.values(), key=lambda p: p.pr):
        if (
            p.pr in taken
            or p.pr in held_members
            or p.state is not EnumLandingPrState.OPEN
            or p.collaborator
            or any(s in CAUSE_EXCLUDED_SUSPENSIONS for s in p.suspensions)
            or not _parked_pr(t, p)
        ):
            continue
        pairs = [
            ModelLandingCausePair(check=c, signature=s, example=x, members=1)
            for c, s, x in _pairs_of(t, p)
        ]
        if not pairs:
            continue  # unread: no signature to key a cause by; it stays parked
        cause = _Cause(
            cause_key(repo_of(p.pr), pairs[0].signature), repo_of(p.pr), pairs, {p.pr}
        )
        if any(c.key == cause.key for c in chosen):
            continue
        chosen.append(cause)
        taken[p.pr] = cause.key
    for cause in chosen:
        _upsert_cause(t, cause)


def _upsert_cause(t: _Tick, cause: _Cause) -> None:
    members = tuple(
        ModelLandingMemberRef(pr=pr, head_sha=t.prs[pr].head_sha)
        for pr in sorted(cause.members)
    )
    rec = t.causes.get(cause.key)
    if rec is not None and rec.outcome is EnumLandingOutcome.CAUSE_NOT_SHARED:
        if _blocks(t, rec):
            return  # not shared for this member set: no cause, members stay per-PR
        rec = None  # a member moved: a fresh cause
    if rec is None:
        rec = ModelLandingCauseRecord(
            key=cause.key, repo=cause.repo, pairs=tuple(cause.pairs), members=members
        )
    elif _active(t, rec):  # new PRs failing an active cause join it
        known = {(p.check, p.signature) for p in rec.pairs}
        pairs = (
            *rec.pairs,
            *(p for p in cause.pairs if (p.check, p.signature) not in known),
        )
        joined = {m.pr: m for m in rec.members}
        for m in members:
            joined.setdefault(m.pr, m)
        rec = rec.model_copy(
            update={
                "pairs": pairs,
                "members": tuple(joined[pr] for pr in sorted(joined)),
            }
        )
    else:
        rec = rec.model_copy(update={"pairs": tuple(cause.pairs), "members": members})
    t.causes[cause.key] = rec
    t.formed.add(cause.key)


def _covering_escalation(
    t: _Tick, rec: ModelLandingCauseRecord
) -> ModelLandingCauseEscalation | None:
    """The ledger MSG that already escalates this cause's open park episode (LC-F3).

    A row covers the episode when it is younger than a park and no RELEASE row
    naming the cause is dated at or after it. The newest such row wins, ties by
    key, so the choice does not depend on input order.
    """
    park = timedelta(hours=t.facts.policy.cause_park_hours)
    rows = [
        r
        for r in t.facts.cause_escalations
        if r.cause == rec.key
        and t.now < r.at + park
        and not any(
            rel.cause == rec.key and rel.at >= r.at for rel in t.facts.cause_releases
        )
    ]
    return max(rows, key=lambda r: (r.at, r.dedupe_key), default=None)


def _park(t: _Tick, rec: ModelLandingCauseRecord) -> None:
    """A spent cause parks; one escalation per park episode, deduped on the ledger row.

    When the ledger already holds this episode's operator MSG (the controller
    died between the append and the state write), the park adopts that row's
    time and key and emits nothing: the state file only caches the answer.
    """
    park = timedelta(hours=t.facts.policy.cause_park_hours)
    row = _covering_escalation(t, rec)
    if row is not None:
        until = row.at + park
        dedupe = row.dedupe_key
    else:
        until = t.now + park
        dedupe = f"{rec.key}@{until.isoformat()}"
    t.causes[rec.key] = rec.model_copy(
        update={"parked_until": until, "escalated": dedupe}
    )
    t.degraded.append(
        ModelLandingDegraded(
            reason=EnumLandingDegradedReason.CAUSE_EXHAUSTED, subject=rec.key
        )
    )
    if row is None and rec.escalated != dedupe:
        t.emit(
            ModelLandingAction(
                kind=EnumLandingActionKind.ESCALATE_OPERATOR,
                subject=rec.key,
                members=rec.members,
                dedupe_key=dedupe,
            )
        )


def _try_dispatch_cause(t: _Tick, rec: ModelLandingCauseRecord) -> bool:
    """One lease per cause key, under the cause caps and inside the pool (D5)."""
    policy = t.facts.policy
    key = rec.key
    if key in t.leases or _parked(t, rec) or _active(t, rec) or _owned(t, rec):
        return False
    if rec.repo in t.draining or t.held(rec.repo):
        return False
    if (
        rec.attempts >= policy.cause_attempt_budget
        or rec.spawn_failures >= policy.cause_spawn_failure_budget
    ):
        _park(t, rec)
        return False
    if t.facts.load1 > policy.load_pause_threshold:
        return False
    if len(t.leases) >= policy.max_workers:
        return False
    cause_leases = [s for s in t.leases if is_cause(s)]
    if len(cause_leases) >= policy.max_cause_workers:
        return False
    if sum(1 for s in cause_leases if repo_of(s) == rec.repo) >= (
        policy.max_cause_workers_per_repo
    ):
        return False
    if (
        sum(1 for s in t.leases if repo_of(s) == rec.repo)
        >= policy.max_workers_per_repo
    ):
        return False
    ladder = policy.cause_engine_ladder
    available = set(t.facts.available_engines)
    engine = next(
        (e for e in ladder[min(rec.attempts, len(ladder) - 1) :] if e in available),
        None,
    )
    if engine is None:
        return False
    lease_id = t.next_lease_id
    t.next_lease_id += 1
    deadline = t.now + timedelta(seconds=policy.cause_lease_seconds)
    head = rec.members[0].head_sha
    checks = tuple(sorted({pair.check for pair in rec.pairs}))
    t.leases[key] = ModelLandingLease(
        pr=key,
        lease_id=lease_id,
        brief_class=EnumLandingBriefClass.SHARED_CAUSE,
        engine=engine,
        dispatched_at=t.now,
        deadline_at=deadline,
        dispatch_head=head,
        seen_heads=(head,),
        last_seen_head=head,
        dispatch_red_checks=checks,
    )
    t.causes[key] = rec.model_copy(
        update={"outcome": None, "reason": EnumLandingOutcomeReason.NONE}
    )
    base = {
        check
        for repo_checks in t.facts.base_red_checks
        if repo_checks.repo == rec.repo
        for check in repo_checks.checks
    }
    t.emit(
        ModelLandingAction(
            kind=EnumLandingActionKind.DISPATCH_WORKER,
            subject=key,
            head_sha=head,
            lease_id=lease_id,
            members=rec.members,
            cause_brief=ModelLandingCauseBrief(
                cause=key,
                repo=rec.repo,
                pairs=rec.pairs,
                members=rec.members,
                base_failing_checks=tuple(c for c in checks if c in base),
                attempt=rec.attempts + 1,
                lease_id=lease_id,
                engine=engine,
                deadline_at=deadline,
                instructions=BRIEF_INSTRUCTIONS[EnumLandingBriefClass.SHARED_CAUSE],
            ),
        )
    )
    return True


def _dispatch_causes(t: _Tick) -> None:
    """Causes go before per-PR workers: floor-breached repos first, then size, then key."""
    floor = set(t.facts.floor_breached_repos)
    order = sorted(
        (t.causes[k] for k in t.formed),
        key=lambda r: (r.repo not in floor, -len(r.members), r.key),
    )
    for rec in order:
        _try_dispatch_cause(t, t.causes[rec.key])
    for rec in t.causes.values():
        if _holds_members(t, rec) or (rec.key in t.formed and _owned(t, rec)):
            t.suppressed.update(m.pr for m in rec.members)


# ----------------------------------------------------------------------- PRs
def _mergeable(p: ModelLandingPrFacts) -> bool:
    return p.ci is EnumLandingCi.GREEN and p.merge_state is EnumLandingMergeState.CLEAN


def _merge(t: _Tick, p: ModelLandingPrFacts) -> None:
    """Merge pinned to the head; a runtime PR lands only holding the token (R6)."""
    if p.runtime:
        in_open_companion = any(
            c.state is EnumLandingPrState.OPEN
            and c.pr not in t.closing
            and any(m.pr == p.pr for m in c.members)
            for c in t.comps.values()
        )
        if in_open_companion:
            t.skips[p.pr] = EnumLandingLandSkipReason.COMPANION_MEMBER
            return
        if t.token is None:
            t.token = p.pr
        elif t.token != p.pr:
            t.skips[p.pr] = EnumLandingLandSkipReason.TOKEN_HELD
            return
    t.emit(
        ModelLandingAction(
            kind=EnumLandingActionKind.MERGE, subject=p.pr, head_sha=p.head_sha
        )
    )


def _stale_cancelled(p: ModelLandingPrFacts) -> bool:
    """BLOCKED with cancelled check copies and no failed check: a stale copy holds it."""
    return (
        p.merge_state is EnumLandingMergeState.BLOCKED
        and p.ci is not EnumLandingCi.PENDING
        and bool(p.cancelled_checks)
        and not p.red_checks
    )


def _refresh_stale_cancelled(
    t: _Tick, p: ModelLandingPrFacts, rec: ModelLandingPrRecord
) -> None:
    """One update-branch per head, at most ``max_stale_refreshes`` per PR; never a worker."""
    if p.head_sha in rec.update_heads:  # refreshed already: wait for the new head
        return
    if rec.stale_refreshes >= t.facts.policy.max_stale_refreshes:
        t.degraded.append(
            ModelLandingDegraded(
                reason=EnumLandingDegradedReason.STALE_REFRESH_EXHAUSTED,
                subject=p.pr,
            )
        )
        return
    t.put_record(
        p.pr,
        update_heads=(*rec.update_heads, p.head_sha),
        stale_refreshes=rec.stale_refreshes + 1,
    )
    t.emit(
        ModelLandingAction(
            kind=EnumLandingActionKind.UPDATE_BRANCH, subject=p.pr, head_sha=p.head_sha
        )
    )


def _park_degraded(t: _Tick, pr: str) -> None:
    t.degraded.append(
        ModelLandingDegraded(
            reason=EnumLandingDegradedReason.ESCALATION_EXHAUSTED, subject=pr
        )
    )


def _brief_class(p: ModelLandingPrFacts, ladder: int) -> EnumLandingBriefClass | None:
    if p.merge_state is EnumLandingMergeState.CONFLICTING:
        return (
            EnumLandingBriefClass.CONFLICT_MECHANICAL
            if ladder == 0
            else EnumLandingBriefClass.CONFLICT_SUBSTANTIVE
        )
    if p.ci is EnumLandingCi.RED:
        if p.runtime:
            return EnumLandingBriefClass.RUNTIME
        if p.red_class is EnumLandingRedClass.CASCADE:
            return EnumLandingBriefClass.CASCADE_RED
        return EnumLandingBriefClass.REAL_RED
    if p.merge_state is EnumLandingMergeState.BEHIND:
        return EnumLandingBriefClass.BEHIND
    return None


def _product_pr(t: _Tick, p: ModelLandingPrFacts) -> None:
    if p.state is not EnumLandingPrState.OPEN:
        return
    suspensions = t.suspensions.get(p.pr, p.suspensions)
    if suspensions or p.collaborator:
        t.skips[p.pr] = _held_reason(suspensions)
        return
    pr = p.pr
    fp = t.fingerprint(pr)
    rec = t.record_of(pr)
    lease = t.leases.get(pr)
    parked = False
    if rec.parked_head is not None:
        if rec.parked_head == p.head_sha and rec.parked_fingerprint == fp:
            parked = True
            _park_degraded(t, pr)
        elif lease is None:
            rec = t.put_record(
                pr, parked_head=None, parked_fingerprint="", ladder_index=0
            )
    if lease is not None:
        # PinMerge: under a live lease, merge only the verified head
        if rec.awaiting_head == p.head_sha and _mergeable(p) and not p.open_parents:
            _merge(t, p)
        else:
            t.skips[p.pr] = (
                EnumLandingLandSkipReason.OPEN_PARENTS
                if p.open_parents
                else EnumLandingLandSkipReason.LEASED
            )
        return
    needs_worker = (
        p.ci is EnumLandingCi.RED or p.merge_state is EnumLandingMergeState.CONFLICTING
    )
    if p.open_parents:  # R3: only chain roots move
        t.skips[pr] = EnumLandingLandSkipReason.OPEN_PARENTS
        if needs_worker and (
            rec.outcome is not EnumLandingOutcome.BLOCKED_ON
            or rec.blocker_fingerprint != fp
        ):
            _record(t, pr, None, EnumLandingOutcome.BLOCKED_ON)
        return
    if (
        p.merge_state is EnumLandingMergeState.BEHIND
        and p.head_sha not in rec.update_heads
    ):
        t.put_record(pr, update_heads=(*rec.update_heads, p.head_sha))
        t.emit(
            ModelLandingAction(
                kind=EnumLandingActionKind.UPDATE_BRANCH,
                subject=pr,
                head_sha=p.head_sha,
            )
        )
        return
    if _mergeable(p):
        _merge(t, p)
        return
    if _stale_cancelled(p):
        _refresh_stale_cancelled(t, p, rec)
        return
    suppressed = rec.outcome in BLOCKED_OUTCOMES and rec.blocker_fingerprint == fp
    if suppressed or parked:
        return
    if (
        p.ci is EnumLandingCi.RED
        and p.red_class in RERUNNABLE_RED_CLASSES
        and p.head_sha not in rec.rerun_heads
    ):
        t.put_record(pr, rerun_heads=(*rec.rerun_heads, p.head_sha))
        t.emit(
            ModelLandingAction(
                kind=EnumLandingActionKind.RERUN, subject=pr, head_sha=p.head_sha
            )
        )
        return
    if pr in t.suppressed:  # a member of a leased, fixing or owned parked cause
        return
    if _brief_class(p, rec.ladder_index) is None:
        return
    if rec.awaiting_head is not None:
        failed = (
            rec.awaiting_head == p.head_sha
            and p.ci is EnumLandingCi.RED
            and bool(set(p.red_checks) & set(rec.attempt_red_checks))
        )
        if failed:  # the fix did not fix it: a failed attempt escalates (R1)
            _escalate(t, pr)
            rec = t.record_of(pr)
            if rec.parked_head is not None:
                _park_degraded(t, pr)
                return
        else:  # a new red on a new head is a first attempt
            rec = t.put_record(pr, awaiting_head=None, ladder_index=0)
    brief_class = _brief_class(p, rec.ladder_index)
    if brief_class is not None:
        _try_dispatch(t, pr, brief_class, p.head_sha, p.red_checks)


# The order a held PR's reason is named in: a person's hold first, the gate last.
_HELD_ORDER: tuple[tuple[EnumLandingSuspension, EnumLandingLandSkipReason], ...] = (
    (EnumLandingSuspension.HOLD, EnumLandingLandSkipReason.HOLD),
    (EnumLandingSuspension.DO_NOT_LAND, EnumLandingLandSkipReason.DO_NOT_LAND),
    (EnumLandingSuspension.DRAFT, EnumLandingLandSkipReason.DRAFT),
    (EnumLandingSuspension.OWNED, EnumLandingLandSkipReason.OWNED),
    (EnumLandingSuspension.GATE, EnumLandingLandSkipReason.GATE),
)


def _held_reason(
    suspensions: tuple[EnumLandingSuspension, ...],
) -> EnumLandingLandSkipReason:
    for suspension, reason in _HELD_ORDER:
        if suspension in suspensions:
            return reason
    return EnumLandingLandSkipReason.COLLABORATOR


def _gate_released(t: _Tick, p: ModelLandingPrFacts) -> bool:
    """A gate whose every reason's premise is a red head holds nothing on a green,
    CLEAN head once it is older than ``stale_gate_seconds`` (two ticks)."""
    return (
        EnumLandingSuspension.GATE in p.suspensions
        and set(p.gate_reasons) <= STALE_WHEN_GREEN_GATE_REASONS
        and _mergeable(p)
        and p.gate_since is not None
        and t.now - p.gate_since > timedelta(seconds=t.facts.policy.stale_gate_seconds)
    )


def _effective_suspensions(t: _Tick) -> None:
    for p in t.prs.values():
        if p.state is EnumLandingPrState.OPEN and _gate_released(t, p):
            t.released.add(p.pr)
            t.suspensions[p.pr] = tuple(
                s for s in p.suspensions if s is not EnumLandingSuspension.GATE
            )
        else:
            t.suspensions[p.pr] = p.suspensions


def _merged_heads(actions: Iterable[ModelLandingAction]) -> set[tuple[str, str | None]]:
    return {
        (a.subject, a.head_sha)
        for a in actions
        if a.kind is EnumLandingActionKind.MERGE
    }


def land_coverage_gaps(
    facts: ModelLandingFacts, decision: ModelLandingDecision
) -> tuple[str, ...]:
    """The open, green, CLEAN PRs with neither a merge on their head nor a named skip.

    Every path of ``_product_pr`` that does not merge an open, green, CLEAN PR
    records a named skip (a suspension or collaborator, a lease, an open parent,
    a companion, the token), so a gap is a defect in this module, not a fleet
    state the caller can produce.
    """
    merged = _merged_heads((*decision.actions, *decision.observed_actions))
    skipped = {(s.pr, s.head_sha) for s in decision.land_skips}
    return tuple(
        sorted(
            p.pr
            for p in facts.prs
            if p.state is EnumLandingPrState.OPEN
            and _mergeable(p)
            and (p.pr, p.head_sha) not in merged
            and (p.pr, p.head_sha) not in skipped
        )
    )


class LandingCoverageError(RuntimeError):
    """A green, CLEAN, open PR left this tick with no merge and no named reason.

    The contract's declared failure outcome: ``decide_landing`` raises it instead
    of returning a decision that silently drops a PR. It is a pure function of the
    facts (same facts, same raise, no side effect), and runtime dispatch reports it
    on the contract's failure terminal event, so the caller acts on nothing.
    """


def _priority(
    t: _Tick, p: ModelLandingPrFacts
) -> tuple[bool, int, bool, datetime, str]:
    """R4: green and mergeable, chain root by PRs unblocked, process-fix, age."""
    unblocks = sum(
        1
        for q in t.prs.values()
        if q.state is EnumLandingPrState.OPEN and p.pr in q.parents
    )
    return (not _mergeable(p), -unblocks, not p.process_fix, p.created_at, p.pr)


# ---------------------------------------------------------------------- tick
def decide_landing(facts: ModelLandingFacts) -> ModelLandingDecision:
    """One tick: facts and the controller's last state in, actions and next state out."""
    state = facts.state
    t = _Tick(
        facts=facts,
        now=facts.observed_at,
        prs={p.pr: p for p in facts.prs},
        comps={c.pr: c for c in facts.companions},
        leases={le.pr: le for le in sorted(state.leases, key=lambda le: le.pr)},
        records={r.pr: r for r in state.records},
        rebuilds={r.key: r for r in state.rebuilds},
        uncovered={u.pr: u for u in state.uncovered},
        revoked=set(state.revoked_lease_ids),
        draining=set(state.draining_repos) | set(facts.drain_requested),
        observe=set(state.observe_only_repos),
        token=state.token_holder,
        next_lease_id=state.next_lease_id,
        close_requested=set(state.close_requested),
        causes={c.key: c for c in state.causes},
        hold=set(facts.fixer_hold),
    )
    _effective_suspensions(t)
    holder = t.prs.get(t.token) if t.token is not None else None
    if t.token is not None and (
        holder is None
        or holder.state is not EnumLandingPrState.OPEN
        or t.suspensions[holder.pr]
        or holder.collaborator
    ):
        t.token = None  # R6: freed first, the tick its holder left
    for result in sorted(facts.results, key=lambda r: (r.lease_id, r.pr)):
        _read_result(t, result)
    for pr in sorted(t.leases):
        _observe_lease(t, t.leases[pr])
    _drain(t)
    _eligibility_reruns(t)
    _advance_rebuilds(t)
    for comp in sorted(t.comps.values(), key=lambda c: c.pr):
        if comp.state is EnumLandingPrState.OPEN:
            _companion(t, comp)
    _cover(t)
    _advance_causes(t)
    _form_causes(t)
    _dispatch_causes(t)
    for p in sorted(t.prs.values(), key=lambda p: _priority(t, p)):
        _product_pr(t, p)
    decision = _decision(t)
    gaps = land_coverage_gaps(facts, decision)
    if gaps:
        raise LandingCoverageError(
            f"green, CLEAN PRs with no merge and no named reason: {', '.join(gaps)}"
        )
    return decision


def _decision(t: _Tick) -> ModelLandingDecision:
    open_subjects = {
        s
        for s, subj in ((s, t.subject(s)) for s in t.records)
        if subj is None or subj.state is EnumLandingPrState.OPEN
    }
    records = tuple(
        t.records[pr]
        for pr in sorted(t.records)
        if pr in open_subjects or pr in t.leases
    )
    reruns = sorted({(e.pr, e.companion) for e in t.elig_reruns})
    next_state = ModelLandingControllerState(
        last_tick=t.facts.tick,
        next_lease_id=t.next_lease_id,
        leases=tuple(t.leases[pr] for pr in sorted(t.leases)),
        records=records,
        revoked_lease_ids=tuple(sorted(t.revoked)),
        token_holder=t.token,
        draining_repos=tuple(sorted(t.draining)),
        observe_only_repos=tuple(sorted(t.observe)),
        rebuilds=tuple(t.rebuilds[k] for k in sorted(t.rebuilds)),
        uncovered=tuple(t.uncovered[pr] for pr in sorted(t.uncovered)),
        eligibility_reruns=tuple(
            ModelLandingEligibilityRerun(pr=pr, companion=c) for pr, c in reruns
        ),
        close_requested=tuple(sorted(t.close_next)),
        causes=tuple(
            t.causes[k] for k in sorted(t.causes) if _keep_cause(t, t.causes[k])
        ),
    )
    return ModelLandingDecision(
        tick=t.facts.tick,
        actions=tuple(t.actions),
        observed_actions=tuple(t.observed),
        recorded_outcomes=tuple(t.recorded),
        degraded=tuple(sorted(t.degraded, key=lambda d: (d.reason.value, d.subject))),
        violations=tuple(t.violations),
        companion_verdicts=tuple(t.verdicts),
        observe_only_refused=tuple(t.refused),
        gates=tuple(
            ModelLandingGateRow(
                pr=p.pr,
                head_sha=p.head_sha,
                reasons=p.gate_reasons,
                since=p.gate_since,
                released=p.pr in t.released,
            )
            for p in sorted(t.prs.values(), key=lambda p: p.pr)
            if p.state is EnumLandingPrState.OPEN
            and EnumLandingSuspension.GATE in p.suspensions
        ),
        land_skips=_land_skips(t),
        next_state=next_state,
    )


def _land_skips(t: _Tick) -> tuple[ModelLandingLandSkip, ...]:
    merged = _merged_heads((*t.actions, *t.observed))
    rows: list[ModelLandingLandSkip] = []
    for pr in sorted(t.skips):
        p = t.prs[pr]
        if not _mergeable(p) or (pr, p.head_sha) in merged:
            continue
        reason = t.skips[pr]
        rows.append(
            ModelLandingLandSkip(
                pr=pr,
                head_sha=p.head_sha,
                reason=reason,
                gate_reasons=(
                    p.gate_reasons if reason is EnumLandingLandSkipReason.GATE else ()
                ),
            )
        )
    return tuple(rows)


class HandlerPrLandingDecision:
    """The landing decision: pure definition-B compute over one tick's facts."""

    def handle(self, request: ModelLandingFacts) -> ModelLandingDecision:
        """Decide one tick; raises ``LandingCoverageError`` (the contract's failure
        outcome) rather than return a decision that leaves a green, CLEAN PR unnamed."""
        return decide_landing(request)


__all__: list[str] = [
    "HandlerPrLandingDecision",
    "LandingCoverageError",
    "blocker_fingerprint",
    "decide_landing",
    "is_cause",
    "land_coverage_gaps",
    "rebuild_key",
    "repo_of",
]
