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
7. Product PRs: chain roots only (R3), blocked-outcome suppression by
   fingerprint (R8), update-branch once per head, one rerun per head (R5),
   the escalation ladder and parking (R1), the token (R6), pinned merge under a
   live lease (``PinMerge``), and dispatch in priority order (R4) under the
   worker pool, the load pause and the available engines (D5).

Actions for an observe-only repo are returned in ``observed_actions`` and never
performed; no worker is dispatched in a draining repo.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    BLOCKED_OUTCOMES,
    RERUNNABLE_RED_CLASSES,
    EnumLandingActionKind,
    EnumLandingBriefClass,
    EnumLandingCi,
    EnumLandingCompanionVerdict,
    EnumLandingDegradedReason,
    EnumLandingEngine,
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
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    BRIEF_INSTRUCTIONS,
    ModelLandingAction,
    ModelLandingCompanionVerdictRow,
    ModelLandingDecision,
    ModelLandingDegraded,
    ModelLandingRecordedOutcome,
    ModelLandingViolation,
    ModelLandingWorkerBrief,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingBlockerRef,
    ModelLandingCompanionFacts,
    ModelLandingFacts,
    ModelLandingPrFacts,
    ModelLandingRefUpdate,
    ModelLandingRerunRun,
    ModelLandingWorkerResult,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
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


def repo_of(subject: str) -> str:
    """The ``owner/name`` of a ``repo#pr`` subject (a repo subject is itself)."""
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
        repo_of(pr) in t.draining
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
    if repo_of(subject) in t.draining or subject in t.leases:
        return False
    if t.facts.load1 > policy.load_pause_threshold:
        return False
    if len(t.leases) >= policy.max_workers:
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
            return
        if t.token is None:
            t.token = p.pr
        elif t.token != p.pr:
            return
    t.emit(
        ModelLandingAction(
            kind=EnumLandingActionKind.MERGE, subject=p.pr, head_sha=p.head_sha
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
    if p.state is not EnumLandingPrState.OPEN or p.suspensions or p.collaborator:
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
        return
    needs_worker = (
        p.ci is EnumLandingCi.RED or p.merge_state is EnumLandingMergeState.CONFLICTING
    )
    if p.open_parents:  # R3: only chain roots move
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
    )
    holder = t.prs.get(t.token) if t.token is not None else None
    if t.token is not None and (
        holder is None
        or holder.state is not EnumLandingPrState.OPEN
        or holder.suspensions
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
    for p in sorted(t.prs.values(), key=lambda p: _priority(t, p)):
        _product_pr(t, p)
    return _decision(t)


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
        next_state=next_state,
    )


class HandlerPrLandingDecision:
    """The landing decision: pure definition-B compute over one tick's facts."""

    def handle(self, request: ModelLandingFacts) -> ModelLandingDecision:
        return decide_landing(request)


__all__: list[str] = [
    "HandlerPrLandingDecision",
    "blocker_fingerprint",
    "decide_landing",
    "rebuild_key",
    "repo_of",
]
