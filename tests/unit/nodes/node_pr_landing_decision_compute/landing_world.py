# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A fake world the landing decision is replayed against, tick by tick.

It holds GitHub truth (PRs, companions, ref updates, reruns), the process
table of every worker the decision dispatched, the workers' result files, a
fake companion producer (idempotent on its key, with injectable failures and
latency) and injected controller crashes. Each tick it applies the scripted
events, builds one ``ModelLandingFacts`` snapshot, calls the decision, checks
the tick's expectations and the model's safety properties, then performs the
decision's actions the way the controller would.

The scenario vocabulary follows ``LandingController.tla``: a fixture lists the
TLC trace it was written from, and each tick names the trace steps it covers.
Heads are written ``h1``, ``h2`` ... per PR and resolved to stable shas.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingCi,
    EnumLandingEngine,
    EnumLandingExternalBlockerKind,
    EnumLandingMergeState,
    EnumLandingPrState,
    EnumLandingPushMode,
    EnumLandingRedClass,
    EnumLandingRefUpdateKind,
    EnumLandingResultKind,
    EnumLandingSuspension,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingAction,
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingBlockerRef,
    ModelLandingCompanionFacts,
    ModelLandingFacts,
    ModelLandingPolicy,
    ModelLandingPrFacts,
    ModelLandingPushEvidence,
    ModelLandingRebuildAck,
    ModelLandingRefUpdate,
    ModelLandingRerunRun,
    ModelLandingWorkerProbe,
    ModelLandingWorkerResult,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    ModelLandingControllerState,
    ModelLandingMemberRef,
)

EPOCH = datetime(2026, 9, 28, 0, 0, 0, tzinfo=UTC)
TICK_SECONDS = 300


def sha(subject: str, n: int) -> str:
    """The stable sha of head ``n`` of ``subject``."""
    return hashlib.sha1(f"{subject}@{n}".encode()).hexdigest()


def head_no(label: str) -> int:
    if not label.startswith("h"):
        raise ValueError(f"head label {label!r} is not hN")
    return int(label[1:])


def repo_of(pr: str) -> str:
    return pr.split("#", 1)[0]


@dataclass
class FakePr:
    pr: str
    created_index: int
    head: int = 1
    state: EnumLandingPrState = EnumLandingPrState.OPEN
    ci: EnumLandingCi = EnumLandingCi.PENDING
    red_class: EnumLandingRedClass | None = None
    red_checks: tuple[str, ...] = ()
    cancelled_checks: tuple[str, ...] = ()
    merge_state: EnumLandingMergeState = EnumLandingMergeState.CLEAN
    suspensions: set[EnumLandingSuspension] = field(default_factory=set)
    collaborator: bool = False
    runtime: bool = False
    process_fix: bool = False
    parents: tuple[str, ...] = ()
    blockers: dict[str, str] = field(default_factory=dict)
    ref_updates: list[ModelLandingRefUpdate] = field(default_factory=list)
    rerun_runs: list[ModelLandingRerunRun] = field(default_factory=list)
    auto_merge_head: int | None = None


@dataclass
class FakeCompanion:
    pr: str
    members: list[tuple[str, int]]
    head: int = 1
    state: EnumLandingPrState = EnumLandingPrState.OPEN
    ci: EnumLandingCi = EnumLandingCi.PENDING
    rebuild_key: str | None = None
    visible_from_tick: int = 0


@dataclass
class FakeWorker:
    lease_id: int
    pr: str
    known_head: int
    procs: set[str] = field(default_factory=lambda: {"main"})
    evidence: list[ModelLandingPushEvidence] = field(default_factory=list)
    result: ModelLandingWorkerResult | None = None
    kill_works: bool = True
    released: bool = False


@dataclass
class FakeProducer:
    fail_next: int = 0
    release: str = "producer-1"
    latency_ticks: int = 1
    idempotent: bool = True
    minted: dict[str, str] = field(default_factory=dict)
    deliveries: Counter[str] = field(default_factory=Counter)
    runs: int = 0


class LandingWorld:
    """One scenario's world, driven tick by tick."""

    def __init__(self, spec: dict[str, Any]) -> None:
        world = spec.get("world", {})
        self.spec = spec
        self.prs: dict[str, FakePr] = {}
        for index, raw in enumerate(world.get("prs", [])):
            pr = FakePr(pr=raw["pr"], created_index=index)
            self._apply_pr_fields(pr, raw)
            self.prs[pr.pr] = pr
        self.companions: dict[str, FakeCompanion] = {}
        for raw in world.get("companions", []):
            members = [
                (m.split("@")[0], head_no(m.split("@")[1])) for m in raw["members"]
            ]
            comp = FakeCompanion(pr=raw["pr"], members=members)
            if "ci" in raw:
                comp.ci = EnumLandingCi(raw["ci"])
            self.companions[comp.pr] = comp
        policy_raw = world.get("policy", {})
        self.policy = ModelLandingPolicy(**policy_raw)
        self.producer = FakeProducer(**world.get("producer", {}))
        self.available_engines = tuple(
            EnumLandingEngine(e)
            for e in world.get(
                "available_engines",
                [e.value for e in ModelLandingPolicy().engine_ladder],
            )
        )
        self.load1: float = float(world.get("load1", 0.0))
        self.workers: dict[int, FakeWorker] = {}
        self.state = ModelLandingControllerState()
        self.tick = 0
        self.now = EPOCH
        self.drain_requested: set[str] = set()
        self.pending_acks: list[ModelLandingRebuildAck] = []
        self.pending_failures: set[str] = set()
        self.eligibility_reruns: Counter[str] = Counter()
        self.decisions: list[ModelLandingDecision] = []
        self.facts_seen: list[ModelLandingFacts] = []
        self.outcomes_by_lease: Counter[int] = Counter()
        self.crash: str | None = None

    # ------------------------------------------------------------------ heads
    def head_sha(self, subject: str, label: str) -> str:
        return sha(subject, head_no(label))

    def label_of(self, subject: str, value: str | None) -> str:
        if value is None:
            return "none"
        for n in range(1, 50):
            if sha(subject, n) == value:
                return f"h{n}"
        return value[:8]

    def _apply_pr_fields(self, pr: FakePr, raw: dict[str, Any]) -> None:
        if "ci" in raw:
            self._set_ci(
                pr,
                raw["ci"],
                raw.get("red_class"),
                raw.get("red_checks"),
                raw.get("cancelled_checks"),
            )
        if "merge_state" in raw:
            pr.merge_state = EnumLandingMergeState(raw["merge_state"])
        for s in raw.get("suspensions", []):
            pr.suspensions.add(EnumLandingSuspension(s))
        pr.collaborator = bool(raw.get("collaborator", pr.collaborator))
        pr.runtime = bool(raw.get("runtime", pr.runtime))
        pr.process_fix = bool(raw.get("process_fix", pr.process_fix))
        pr.parents = tuple(raw.get("parents", pr.parents))
        pr.blockers.update(raw.get("blockers", {}))

    @staticmethod
    def _set_ci(
        pr: FakePr,
        ci: str,
        red_class: str | None,
        red_checks: list[str] | None,
        cancelled_checks: list[str] | None = None,
    ) -> None:
        pr.ci = EnumLandingCi(ci)
        pr.cancelled_checks = tuple(cancelled_checks or ())
        if pr.ci is EnumLandingCi.RED:
            pr.red_class = EnumLandingRedClass(red_class or "product")
            pr.red_checks = tuple(["unit"] if red_checks is None else red_checks)
        else:
            pr.red_class = None
            pr.red_checks = ()

    def _new_head(
        self, pr: FakePr, kind: EnumLandingRefUpdateKind, at: datetime
    ) -> None:
        before = sha(pr.pr, pr.head)
        pr.head += 1
        pr.ref_updates.append(
            ModelLandingRefUpdate(
                before_sha=before, after_sha=sha(pr.pr, pr.head), kind=kind, at=at
            )
        )
        pr.ci = EnumLandingCi.PENDING
        pr.red_class = None
        pr.red_checks = ()
        pr.cancelled_checks = ()

    def _worker_for(self, pr: str, lease: int | None = None) -> FakeWorker:
        if lease is not None:
            return self.workers[lease]
        live = [w for w in self.workers.values() if w.pr == pr and not w.released]
        if not live:
            live = [w for w in self.workers.values() if w.pr == pr]
        return max(live, key=lambda w: w.lease_id)

    # ----------------------------------------------------------------- events
    def apply_event(self, event: dict[str, Any]) -> None:
        ((name, arg),) = event.items()
        arg = arg or {}
        handler = getattr(self, f"_ev_{name}")
        handler(arg)

    def _ev_clock(self, arg: dict[str, Any]) -> None:
        self.now += timedelta(seconds=int(arg["seconds"]))

    def _ev_ci(self, arg: dict[str, Any]) -> None:
        self._set_ci(
            self.prs[arg["pr"]],
            arg["result"],
            arg.get("red_class"),
            arg.get("red_checks"),
            arg.get("cancelled_checks"),
        )

    def _ev_companion_ci(self, arg: dict[str, Any]) -> None:
        self.companions[arg["companion"]].ci = EnumLandingCi(arg["result"])

    def _ev_companion_merged_elsewhere(self, arg: dict[str, Any]) -> None:
        self.companions[arg["companion"]].state = EnumLandingPrState.MERGED

    def _ev_merge_state(self, arg: dict[str, Any]) -> None:
        self.prs[arg["pr"]].merge_state = EnumLandingMergeState(arg["state"])

    def _ev_member_push(self, arg: dict[str, Any]) -> None:
        self._new_head(
            self.prs[arg["pr"]], EnumLandingRefUpdateKind.FAST_FORWARD, self.now
        )

    def _ev_member_close(self, arg: dict[str, Any]) -> None:
        self.prs[arg["pr"]].state = EnumLandingPrState.CLOSED

    def _ev_member_merged_elsewhere(self, arg: dict[str, Any]) -> None:
        self.prs[arg["pr"]].state = EnumLandingPrState.MERGED

    def _ev_hold(self, arg: dict[str, Any]) -> None:
        self.prs[arg["pr"]].suspensions.add(EnumLandingSuspension.HOLD)

    def _ev_release(self, arg: dict[str, Any]) -> None:
        self.prs[arg["pr"]].suspensions.discard(EnumLandingSuspension.HOLD)

    def _ev_blocker(self, arg: dict[str, Any]) -> None:
        self.prs[arg["pr"]].blockers[arg["ref"]] = arg["state"]

    def _ev_drain(self, arg: dict[str, Any]) -> None:
        self.drain_requested.add(arg["repo"])

    def _ev_producer_fail_next(self, arg: dict[str, Any]) -> None:
        self.producer.fail_next = int(arg["count"])

    def _ev_producer_release(self, arg: dict[str, Any]) -> None:
        self.producer.release = str(arg["release"])

    def _ev_load(self, arg: dict[str, Any]) -> None:
        self.load1 = float(arg["load1"])

    def _ev_engines(self, arg: dict[str, Any]) -> None:
        self.available_engines = tuple(EnumLandingEngine(e) for e in arg["available"])

    def _ev_crash(self, arg: dict[str, Any]) -> None:
        self.crash = str(arg["point"])

    def _ev_kill_fails(self, arg: dict[str, Any]) -> None:
        self._worker_for(arg["pr"]).kill_works = False

    def _ev_child_spawn(self, arg: dict[str, Any]) -> None:
        worker = self._worker_for(arg["pr"])
        assert "main" in worker.procs, "only a live worker spawns a child"
        worker.procs.add("child_setsid" if arg.get("setsid") else "child")

    def _ev_process_exit(self, arg: dict[str, Any]) -> None:
        worker = self._worker_for(arg["pr"], arg.get("lease"))
        proc = arg.get("process", "main")
        assert proc in worker.procs, f"{proc} is not alive"
        worker.procs.discard(proc)

    def _ev_foreign_rewrite(self, arg: dict[str, Any]) -> None:
        at = self.now + timedelta(seconds=int(arg.get("at_offset_seconds", 0)))
        self._new_head(self.prs[arg["pr"]], EnumLandingRefUpdateKind.FORCE_PUSH, at)

    def _ev_worker_push(self, arg: dict[str, Any]) -> None:
        pr = self.prs[arg["pr"]]
        worker = self._worker_for(arg["pr"])
        assert worker.procs, "a dead worker cannot push"
        mode = EnumLandingPushMode(arg.get("mode", "fast_forward"))
        kind = (
            EnumLandingRefUpdateKind.FAST_FORWARD
            if mode is EnumLandingPushMode.FAST_FORWARD
            else EnumLandingRefUpdateKind.FORCE_PUSH
        )
        old = sha(pr.pr, worker.known_head)
        self._new_head(pr, kind, self.now)
        claimed_old: str | None = old
        if "claim_old" in arg:
            claimed_old = self.head_sha(pr.pr, arg["claim_old"])
        if mode is EnumLandingPushMode.BARE_FORCE:
            claimed_old = None
        reported = EnumLandingPushMode(arg.get("report_mode", mode.value))
        worker.evidence.append(
            ModelLandingPushEvidence(
                ref=f"refs/heads/{pr.pr}",
                expected_old_head=claimed_old,
                new_head=sha(pr.pr, pr.head),
                mode=reported,
            )
        )
        worker.known_head = pr.head
        if "merge_state" in arg:
            pr.merge_state = EnumLandingMergeState(arg["merge_state"])

    def _ev_worker_rerun(self, arg: dict[str, Any]) -> None:
        pr = self.prs[arg["pr"]]
        run_id = 9000 + len(pr.rerun_runs)
        pr.rerun_runs.append(
            ModelLandingRerunRun(
                run_id=run_id, head_sha=sha(pr.pr, pr.head), created_at=self.now
            )
        )
        pr.ci = EnumLandingCi.PENDING
        pr.red_class = None
        pr.red_checks = ()
        pr.cancelled_checks = ()

    def _ev_worker_result(self, arg: dict[str, Any]) -> None:
        pr = self.prs.get(arg["pr"])
        subject = arg["pr"]
        worker = self._worker_for(subject, arg.get("lease"))
        kind = EnumLandingResultKind(arg["kind"])
        live_head = pr.head if pr is not None else self.companions[subject].head
        head_label = arg.get("head")
        head = (
            self.head_sha(subject, head_label)
            if head_label
            else sha(subject, live_head)
        )
        evidence_spec = arg.get("evidence", "auto")
        evidence: tuple[ModelLandingPushEvidence, ...]
        if evidence_spec == "auto":
            evidence = tuple(worker.evidence)
        elif evidence_spec == "none":
            evidence = ()
        else:
            evidence = tuple(
                ModelLandingPushEvidence(
                    ref=f"refs/heads/{subject}",
                    expected_old_head=self.head_sha(subject, e["old"])
                    if e.get("old")
                    else None,
                    new_head=self.head_sha(subject, e["new"]),
                    mode=EnumLandingPushMode(e["mode"]),
                )
                for e in evidence_spec
            )
        rerun_id = None
        if arg.get("rerun") and pr is not None:
            rerun_id = pr.rerun_runs[-1].run_id
        worker.result = ModelLandingWorkerResult(
            lease_id=worker.lease_id,
            pr=subject,
            kind=kind,
            head_sha=head if kind is not EnumLandingResultKind.UNPARSEABLE else None,
            push_evidence=evidence,
            rerun_run_id=rerun_id,
            blocker_kind=EnumLandingExternalBlockerKind(arg["blocker_kind"])
            if "blocker_kind" in arg
            else None,
            blocker_ref=arg.get("blocker_ref"),
        )

    # ------------------------------------------------------------------ facts
    def facts(self) -> ModelLandingFacts:
        prs = []
        for pr in sorted(self.prs.values(), key=lambda p: p.pr):
            parents_open = tuple(
                p
                for p in pr.parents
                if p in self.prs and self.prs[p].state is EnumLandingPrState.OPEN
            )
            refs = [
                ModelLandingBlockerRef(ref=r, state=s)
                for r, s in sorted(pr.blockers.items())
            ]
            for parent in pr.parents:
                if parent in self.prs:
                    par = self.prs[parent]
                    refs.append(
                        ModelLandingBlockerRef(
                            ref=parent,
                            state=par.state.value,
                            head_sha=sha(parent, par.head),
                        )
                    )
            stale = (
                {"cancelled_checks": pr.cancelled_checks} if pr.cancelled_checks else {}
            )
            prs.append(
                ModelLandingPrFacts(
                    **stale,
                    pr=pr.pr,
                    head_sha=sha(pr.pr, pr.head),
                    state=pr.state,
                    ci=pr.ci,
                    red_class=pr.red_class,
                    red_checks=pr.red_checks,
                    merge_state=pr.merge_state,
                    suspensions=tuple(sorted(pr.suspensions)),
                    collaborator=pr.collaborator,
                    runtime=pr.runtime,
                    process_fix=pr.process_fix,
                    created_at=EPOCH
                    - timedelta(days=30)
                    + timedelta(hours=pr.created_index),
                    parents=pr.parents,
                    open_parents=parents_open,
                    blocker_refs=tuple(refs),
                    ref_updates=tuple(pr.ref_updates),
                    rerun_runs=tuple(pr.rerun_runs),
                    auto_merge_head=sha(pr.pr, pr.auto_merge_head)
                    if pr.auto_merge_head
                    else None,
                )
            )
        comps = [
            ModelLandingCompanionFacts(
                pr=c.pr,
                head_sha=sha(c.pr, c.head),
                state=c.state,
                ci=c.ci,
                members=tuple(
                    ModelLandingMemberRef(pr=m, head_sha=sha(m, h))
                    for m, h in c.members
                ),
                rebuild_key=c.rebuild_key,
            )
            for c in sorted(self.companions.values(), key=lambda c: c.pr)
            if c.visible_from_tick <= self.tick
        ]
        probes = []
        results = []
        for lease in self.state.leases:
            worker = self.workers.get(lease.lease_id)
            procs = worker.procs if worker else set()
            probes.append(
                ModelLandingWorkerProbe(
                    lease_id=lease.lease_id,
                    group_alive=bool(procs & {"main", "child"}),
                    tagged_alive=bool(procs),
                )
            )
        for worker in sorted(self.workers.values(), key=lambda w: w.lease_id):
            if worker.result is not None:
                results.append(worker.result)
        return ModelLandingFacts(
            tick=self.tick,
            observed_at=self.now,
            policy=self.policy,
            state=self.state,
            prs=tuple(prs),
            companions=tuple(comps),
            probes=tuple(probes),
            results=tuple(results),
            rebuild_acks=tuple(self.pending_acks),
            producer_failures=tuple(sorted(self.pending_failures)),
            producer_release=self.producer.release,
            drain_requested=tuple(sorted(self.drain_requested)),
            load1=self.load1,
            available_engines=self.available_engines,
        )

    # ---------------------------------------------------------------- actions
    def label(self, action: ModelLandingAction) -> str:
        kind = action.kind
        text = f"{kind.value} {action.subject}"
        if kind is EnumLandingActionKind.MERGE:
            text += f" {self.label_of(action.subject, action.head_sha)}"
        elif kind is EnumLandingActionKind.COMPANION_REBUILD:
            members = ",".join(
                f"{m.pr}@{self.label_of(m.pr, m.head_sha)}"
                for m in sorted(action.members, key=lambda m: m.pr)
            )
            text += f" [{members}]"
        elif kind is EnumLandingActionKind.DISPATCH_WORKER:
            assert action.brief is not None
            text += f" {action.brief.brief_class.value}"
        elif kind is EnumLandingActionKind.COMPANION_CLOSE and action.replacement:
            text += f" by {action.replacement}"
        return text

    def _perform(self, action: ModelLandingAction) -> None:
        kind = action.kind
        if kind is EnumLandingActionKind.MERGE:
            if action.subject in self.prs:
                pr = self.prs[action.subject]
                if (
                    pr.state is EnumLandingPrState.OPEN
                    and sha(pr.pr, pr.head) == action.head_sha
                    and pr.ci is EnumLandingCi.GREEN
                ):
                    pr.state = EnumLandingPrState.MERGED
            else:
                comp = self.companions[action.subject]
                if (
                    comp.state is EnumLandingPrState.OPEN
                    and sha(comp.pr, comp.head) == action.head_sha
                ):
                    comp.state = EnumLandingPrState.MERGED
        elif kind is EnumLandingActionKind.RERUN:
            self._ev_worker_rerun({"pr": action.subject})
        elif kind is EnumLandingActionKind.UPDATE_BRANCH:
            pr = self.prs[action.subject]
            if self.spec.get("update_branch_result") == "refused":
                return
            self._new_head(pr, EnumLandingRefUpdateKind.FAST_FORWARD, self.now)
            pr.merge_state = EnumLandingMergeState(
                self.spec.get("update_branch_result", "clean")
            )
        elif kind is EnumLandingActionKind.ELIGIBILITY_RERUN:
            self.eligibility_reruns[action.subject] += 1
        elif kind is EnumLandingActionKind.COMPANION_CLOSE:
            comp = self.companions[action.subject]
            if not self.spec.get("close_fails"):
                comp.state = EnumLandingPrState.CLOSED
        elif kind is EnumLandingActionKind.DISPATCH_WORKER:
            assert action.brief is not None
            assert action.lease_id is not None
            subject = action.subject
            head = (
                self.prs[subject].head
                if subject in self.prs
                else self.companions[subject].head
            )
            self.workers[action.lease_id] = FakeWorker(
                lease_id=action.lease_id, pr=subject, known_head=head
            )
        elif kind is EnumLandingActionKind.KILL_WORKER:
            assert action.lease_id is not None
            worker = self.workers.get(action.lease_id)
            if worker is not None and worker.kill_works:
                worker.procs.clear()
        elif kind is EnumLandingActionKind.DISCARD_RESULT:
            assert action.lease_id is not None
            self.workers[action.lease_id].result = None
        elif kind is EnumLandingActionKind.COMPANION_REBUILD:
            self._deliver(action)
        elif kind is EnumLandingActionKind.OBSERVE_ONLY:
            pass
        else:  # pragma: no cover - exhaustive
            raise AssertionError(kind)

    def _deliver(self, action: ModelLandingAction) -> None:
        assert action.rebuild_key is not None
        assert action.target_repo is not None
        key = action.rebuild_key
        if self.crash == "before_delivery":
            return
        producer = self.producer
        producer.deliveries[key] += 1
        producer.runs += 1
        run_id = f"run-{producer.runs}"
        if producer.fail_next > 0:
            producer.fail_next -= 1
            self.pending_failures.add(key)
        elif key in producer.minted and producer.idempotent:
            pass  # returns the companion it already minted
        else:
            number = 100 + len(producer.minted) + 1
            pr = f"{action.target_repo}#{number}"
            producer.minted[key] = pr
            self.companions[pr] = FakeCompanion(
                pr=pr,
                members=[(m.pr, self.prs[m.pr].head) for m in action.members],
                rebuild_key=key,
                visible_from_tick=self.tick + producer.latency_ticks,
            )
        if self.crash != "after_delivery":
            self.pending_acks.append(ModelLandingRebuildAck(key=key, run_id=run_id))

    # ------------------------------------------------------------------- tick
    def step(self, tick_spec: dict[str, Any]) -> ModelLandingDecision:
        self.tick += 1
        self.now += timedelta(seconds=TICK_SECONDS)
        self.crash = None
        for event in tick_spec.get("events", []):
            self.apply_event(event)
        facts = self.facts()
        self.facts_seen.append(facts)
        # the snapshot's acks and failures are consumed by this tick
        self.pending_acks = []
        self.pending_failures = set()
        decision = decide_landing(facts)
        again = decide_landing(facts)
        assert decision.model_dump_json() == again.model_dump_json(), (
            "decide is not deterministic"
        )
        self._check_properties(facts, decision)
        self.decisions.append(decision)
        # the tick read every result file it was shown
        for result in facts.results:
            self.workers[result.lease_id].result = None
        # write-ahead: the next state is written before any action is performed
        self.state = decision.next_state
        live = {lease.lease_id for lease in self.state.leases}
        for worker in self.workers.values():
            if worker.lease_id not in live and any(
                lease.lease_id == worker.lease_id for lease in facts.state.leases
            ):
                worker.released = True
        for action in decision.actions:
            self._perform(action)
        return decision

    # ------------------------------------------------------------- properties
    def _check_properties(
        self, facts: ModelLandingFacts, decision: ModelLandingDecision
    ) -> None:
        state = decision.next_state
        before = {lease.lease_id: lease for lease in facts.state.leases}
        after = {lease.lease_id: lease for lease in state.leases}
        probe = {p.lease_id: p for p in facts.probes}
        # P1: one lease per PR, keyed repo#pr
        prs = [lease.pr for lease in state.leases]
        assert len(prs) == len(set(prs)), f"P1: two leases on one PR: {prs}"
        # P8: a lease is released only with its worker's processes gone
        for lease_id in before.keys() - after.keys():
            p = probe[lease_id]
            assert not p.group_alive, f"P8: lease {lease_id} released alive"
            assert not p.tagged_alive, f"P8: lease {lease_id} released tagged"
        # P1 over processes: never two live workers on one PR
        alive_by_pr: Counter[str] = Counter()
        for worker in self.workers.values():
            if worker.procs:
                alive_by_pr[worker.pr] += 1
        for action in decision.actions:
            if action.kind is EnumLandingActionKind.DISPATCH_WORKER:
                assert alive_by_pr[action.subject] == 0, (
                    f"P1: dispatch over a live worker on {action.subject}"
                )
        # P3: each lease reaches at most one recorded outcome
        for rec in decision.recorded_outcomes:
            if rec.lease_id is not None:
                self.outcomes_by_lease[rec.lease_id] += 1
                assert self.outcomes_by_lease[rec.lease_id] == 1, (
                    f"P3: lease {rec.lease_id} twice"
                )
        # P2: the token never stays with a PR that left
        if state.token_holder is not None:
            holder = self.prs[state.token_holder]
            assert holder.state is EnumLandingPrState.OPEN, "P2: holder left"
            assert not holder.suspensions, "P2: holder dequeued"
        # P7: no dispatch in a draining repo; observe-only only with no lease in it
        for action in decision.actions:
            if action.kind is EnumLandingActionKind.DISPATCH_WORKER:
                assert repo_of(action.subject) not in state.draining_repos, (
                    "P7: dispatch while draining"
                )
            if action.kind is EnumLandingActionKind.OBSERVE_ONLY:
                assert not any(
                    repo_of(lease.pr) == action.subject for lease in state.leases
                ), "P7"
        # NoUnverifiedLand: under a live lease, merge only the verified head
        records = {r.pr: r for r in state.records}
        leased = {lease.pr for lease in state.leases}
        for action in decision.actions:
            if action.kind is EnumLandingActionKind.MERGE and action.subject in leased:
                rec = records.get(action.subject)
                assert rec is not None, "merge under a lease with no record"
                assert rec.awaiting_head == action.head_sha, "merge not pinned"
        # P9: at most one companion per rebuild key
        keys = Counter(c.rebuild_key for c in self.companions.values() if c.rebuild_key)
        assert all(n == 1 for n in keys.values()), f"P9: {keys}"

    # ------------------------------------------------------------ expectation
    def check(
        self, decision: ModelLandingDecision, expect: dict[str, Any], where: str
    ) -> None:
        state = decision.next_state
        if "actions" in expect:
            got = sorted(self.label(a) for a in decision.actions)
            assert got == sorted(expect["actions"]), f"{where}: actions {got}"
        if "observed" in expect:
            got = sorted(self.label(a) for a in decision.observed_actions)
            assert got == sorted(expect["observed"]), f"{where}: observed {got}"
        if "recorded" in expect:
            got = sorted(
                f"{r.pr} {r.outcome.value} {r.reason.value}"
                for r in decision.recorded_outcomes
            )
            assert got == sorted(expect["recorded"]), f"{where}: recorded {got}"
        for pr, spec in expect.get("lease", {}).items():
            lease = next((le for le in state.leases if le.pr == pr), None)
            if spec is None:
                assert lease is None, f"{where}: lease on {pr} still held"
                continue
            assert lease is not None, f"{where}: no lease on {pr}"
            for key, value in spec.items():
                if key == "last_seen":
                    assert self.label_of(pr, lease.last_seen_head) == value, (
                        f"{where}: last_seen"
                    )
                else:
                    assert getattr(lease, key) == value, (
                        f"{where}: lease {key}={getattr(lease, key)}"
                    )
        if "lease_count" in expect:
            assert len(state.leases) == expect["lease_count"], f"{where}: lease count"
        for pr, value in expect.get("outcome", {}).items():
            rec = next(r for r in state.records if r.pr == pr)
            got = f"{rec.outcome.value if rec.outcome else 'none'}/{rec.reason.value}"
            assert got == value, f"{where}: outcome {pr} {got}"
        for pr, value in expect.get("ladder", {}).items():
            rec = next(r for r in state.records if r.pr == pr)
            assert rec.ladder_index == value, f"{where}: ladder {pr} {rec.ladder_index}"
        for pr, value in expect.get("awaiting", {}).items():
            rec = next(r for r in state.records if r.pr == pr)
            assert self.label_of(pr, rec.awaiting_head) == value, (
                f"{where}: awaiting {pr}"
            )
        for pr, engine in expect.get("engine", {}).items():
            dispatch = [
                a
                for a in decision.actions
                if a.kind is EnumLandingActionKind.DISPATCH_WORKER and a.subject == pr
            ]
            assert dispatch, f"{where}: no dispatch of {pr}"
            assert dispatch[0].brief is not None, where
            assert dispatch[0].brief.engine.value == engine, (
                f"{where}: engine {dispatch[0].brief.engine}"
            )
        if "degraded" in expect:
            got = sorted(
                f"{d.reason.value} {d.subject.split(' ')[0]}" for d in decision.degraded
            )
            assert got == sorted(expect["degraded"]), f"{where}: degraded {got}"
        if "violations" in expect:
            assert len(decision.violations) == expect["violations"], (
                f"{where}: violations"
            )
        if "rebuilds" in expect:
            got = sorted(
                (
                    f"[{','.join(f'{m.pr}@{self.label_of(m.pr, m.head_sha)}' for m in r.members)}]"
                    f" {r.status.value} {r.attempts}"
                )
                for r in state.rebuilds
            )
            assert got == sorted(expect["rebuilds"]), f"{where}: rebuilds {got}"
        for comp, verdict in expect.get("verdicts", {}).items():
            row = next(v for v in decision.companion_verdicts if v.companion == comp)
            assert row.verdict.value == verdict, (
                f"{where}: verdict {comp} {row.verdict}"
            )
        if "observe_only_refused" in expect:
            assert (
                list(decision.observe_only_refused) == expect["observe_only_refused"]
            ), where
        if "token" in expect:
            assert state.token_holder == expect["token"], (
                f"{where}: token {state.token_holder}"
            )
        if "uncovered" in expect:
            got = sorted(u.pr for u in state.uncovered)
            assert got == sorted(expect["uncovered"]), f"{where}: uncovered {got}"

    def check_end(self, end: dict[str, Any]) -> None:
        for pr, n in end.get("eligibility_reruns", {}).items():
            assert self.eligibility_reruns[pr] == n, f"end: eligibility reruns {pr}"
        if "companions_per_key" in end:
            keys = Counter(
                c.rebuild_key for c in self.companions.values() if c.rebuild_key
            )
            assert sorted(keys.values()) == end["companions_per_key"], f"end: {keys}"
        if "deliveries" in end:
            assert sorted(self.producer.deliveries.values()) == end["deliveries"], (
                f"end: deliveries {self.producer.deliveries}"
            )
        for pr, state in end.get("pr_state", {}).items():
            got = (
                self.prs[pr].state.value
                if pr in self.prs
                else self.companions[pr].state.value
            )
            assert got == state, f"end: {pr} {got}"
        if "dispatches" in end:
            got: Counter[str] = Counter()
            for d in self.decisions:
                for a in d.actions:
                    if a.kind is EnumLandingActionKind.DISPATCH_WORKER:
                        got[a.subject] += 1
            assert dict(got) == end["dispatches"], f"end: dispatches {dict(got)}"


def run_scenario(spec: dict[str, Any]) -> LandingWorld:
    """Replay one scenario; every tick's expectations and the properties must hold."""
    world = LandingWorld(spec)
    for index, tick_spec in enumerate(spec["ticks"], start=1):
        decision = world.step(tick_spec)
        world.check(
            decision, tick_spec.get("expect", {}), f"{spec.get('id', '?')} tick {index}"
        )
    world.check_end(spec.get("end", {}))
    return world
