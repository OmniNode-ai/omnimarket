# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared red causes: one bounded cause worker for many red PRs.

Selectors (one ``-k`` each): ``replay_tick722`` replays the landing controller's
snapshot of 2026-10-03 (one omnimarket cause, at least 16 members, no per-PR
dispatch for a member, omnimarket#3321 inside); ``cause_permutation`` checks
the decision is byte-identical under input permutation; ``cause_scenario``
runs S21 to S28 (the cause scenarios of the landing controller model, taken
from the scenario text of the model task while its TLC verdict is pending);
``budget``, ``fixer_hold`` and ``escalated_singleton`` pin the bounds.

The replay fixture ``fixtures/replay_2026_10_03_omnimarket.json`` is the
controller's facts snapshot of tick 725 (observed 2026-10-03T21:00:21Z, the
input of that tick; the same cluster as tick 722), trimmed to the 25 omnimarket
PRs, the 5 live leases with their probes, and the records of those subjects.
Two fields the snapshot does not carry were derived, never invented:

* ``red_annotations``: for each red check, the failure-level annotations (at
  most 3, newline-joined, in order) of the latest failing check-run copy on
  the snapshot head, from the GitHub check rollup read at about 21:05Z. A red
  check with no failing copy in that read (``CI Summary``, ``Coverage Sweep
  Gate`` and ``OCC Companion Merged Gate`` past the rollup's first 100
  contexts) is absent, so it reads ``unread`` and never clusters.
* ``gate_since``: for each ``gate``-suspended PR, the ``completed_at`` of the
  first-created (lowest id) failing check-run copy of its red checks on the
  snapshot head, read from the GitHub check-runs API by the ids the rollup read
  carries: the moment that head first read red. The controller's tick tail
  confirms every such PR in its companion wait from tick 721 (20:36:14Z), so
  the true wait is at least 24 minutes and at most this bound;
  ``test_replay_tick722_lower_bound_gate_still_forms_a_cause`` replays the
  lower bound too.

Author handles inside annotation text (branch prefixes) are replaced by role
names; the floor alarm's breach set is not in the snapshot and reads as no
breach.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingAction,
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    ModelLandingControllerState,
)

FIXTURE = Path(__file__).parent / "fixtures" / "replay_2026_10_03_omnimarket.json"
OMNIMARKET = "OmniNode-ai/omnimarket"
REPO = "acme/app"
OTHER = "acme/lib"
EPOCH = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
TICK = timedelta(seconds=300)
CHECK = "repo-evidence / dod-verify"
ANN = (
    "Process completed with exit code 1.\n"
    "OMN-17427 [dod-a, dod-b]: bound test also passes at the merge base: the "
    "control did not fail (always-pass)"
)
EMPTY_FINGERPRINT = hashlib.sha256(b"").hexdigest()


def sha(subject: str, n: int) -> str:
    return hashlib.sha1(f"{subject}@{n}".encode()).hexdigest()


def key_for(check: str = CHECK, text: str = ANN, repo: str = REPO) -> str:
    from omnimarket.handlers.cause_signature import cause_key, normalize_signature

    return cause_key(repo, normalize_signature(check, text))


@dataclass
class Pr:
    number: int
    repo: str = REPO
    head: int = 1
    state: str = "open"
    ci: str = "red"
    red_class: str = "product"
    red_checks: tuple[str, ...] = (CHECK,)
    annotations: dict[str, str] = field(default_factory=lambda: {CHECK: ANN})
    suspensions: tuple[str, ...] = ()
    gate_since: datetime | None = None
    gate_reasons: tuple[str, ...] = ()
    process_fix: bool = False
    rerun_runs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def head_sha(self) -> str:
        return sha(self.key, self.head)

    def facts(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "pr": self.key,
            "head_sha": self.head_sha,
            "state": self.state,
            "ci": self.ci,
            "created_at": (EPOCH - timedelta(days=1, minutes=-self.number)).isoformat(),
            "suspensions": list(self.suspensions),
            "process_fix": self.process_fix,
            "rerun_runs": list(self.rerun_runs),
        }
        if self.ci == "red":
            data["red_class"] = self.red_class
            data["red_checks"] = list(self.red_checks)
            data["red_annotations"] = dict(self.annotations)
        if self.gate_since is not None:
            data["gate_since"] = self.gate_since.isoformat()
        if "gate" in self.suspensions:
            data["gate_reasons"] = list(self.gate_reasons or ("companion_wait",))
        return data


def green(number: int, **kw: Any) -> Pr:
    return Pr(number, ci="green", red_checks=(), annotations={}, **kw)


def parked_record(pr: Pr) -> dict[str, Any]:
    """The record of a PR parked escalation_exhausted on its head (R1)."""
    return {
        "pr": pr.key,
        "ladder_index": 3,
        "outcome": "timed_out",
        "reason": "exited",
        "parked_head": pr.head_sha,
        "parked_fingerprint": EMPTY_FINGERPRINT,
    }


class CauseWorld:
    """GitHub truth, worker processes and the controller state, tick by tick."""

    def __init__(
        self,
        prs: list[Pr],
        policy: dict[str, Any] | None = None,
        records: list[dict[str, Any]] | None = None,
        **extra: Any,
    ) -> None:
        self.prs = {p.key: p for p in prs}
        self.policy = {"max_workers": 18, **(policy or {})}
        self.state = ModelLandingControllerState.model_validate(
            {"records": records or []}
        )
        self.extra: dict[str, Any] = dict(extra)
        self.tick = 0
        self.now = EPOCH
        self.alive: dict[int, bool] = {}
        self.kill_works: dict[int, bool] = {}
        self.results: list[dict[str, Any]] = []
        self.crash_spawn = False
        self.decisions: list[ModelLandingDecision] = []

    def facts(self) -> ModelLandingFacts:
        probes = [
            {
                "lease_id": le.lease_id,
                "group_alive": self.alive.get(le.lease_id, False),
                "tagged_alive": self.alive.get(le.lease_id, False),
            }
            for le in self.state.leases
        ]
        return ModelLandingFacts.model_validate(
            {
                "tick": self.tick,
                "observed_at": self.now.isoformat(),
                "policy": self.policy,
                "state": self.state.model_dump(mode="json"),
                "prs": [self.prs[k].facts() for k in sorted(self.prs)],
                "probes": probes,
                "results": self.results,
                **self.extra,
            }
        )

    def step(self, advance: timedelta = TICK) -> ModelLandingDecision:
        self.tick += 1
        self.now += advance
        facts = self.facts()
        decision = decide_landing(facts)
        assert decide_landing(facts).model_dump_json() == decision.model_dump_json()
        self._check(facts, decision)
        self.results = []
        self.state = decision.next_state
        for action in decision.actions:
            self._perform(action)
        self.decisions.append(decision)
        return decision

    def _check(self, facts: ModelLandingFacts, decision: ModelLandingDecision) -> None:
        state = decision.next_state
        subjects = [le.pr for le in state.leases]
        assert len(subjects) == len(set(subjects)), (
            f"two leases on one subject {subjects}"
        )
        released = {le.lease_id for le in facts.state.leases} - {
            le.lease_id for le in state.leases
        }
        for lease_id in released:
            assert not self.alive.get(lease_id, False), (
                f"lease {lease_id} released alive"
            )
        leased = {le.pr for le in state.leases}
        held: set[str] = set()
        for rec in state.causes:
            not_shared = (
                rec.outcome is not None and rec.outcome.value == "cause_not_shared"
            )
            if rec.key in leased and not not_shared:
                held |= {m.pr for m in rec.members}
        for action in decision.actions:
            if action.kind.value == "dispatch_worker" and not is_cause_subject(
                action.subject
            ):
                assert action.subject not in held, (
                    f"per-PR dispatch of {action.subject}, a member of a leased cause"
                )

    def _perform(self, action: ModelLandingAction) -> None:
        kind = action.kind.value
        if kind == "dispatch_worker":
            assert action.lease_id is not None
            self.alive[action.lease_id] = not self.crash_spawn
        elif kind == "kill_worker":
            assert action.lease_id is not None
            if self.kill_works.get(action.lease_id, True):
                self.alive[action.lease_id] = False
        elif kind == "rerun":
            pr = self.prs[action.subject]
            pr.ci = "pending"
            pr.rerun_runs.append(
                {
                    "run_id": 9000 + len(pr.rerun_runs) + 1,
                    "head_sha": pr.head_sha,
                    "created_at": self.now.isoformat(),
                }
            )
        elif kind == "merge":
            pr = self.prs[action.subject]
            if pr.ci == "green" and pr.head_sha == action.head_sha:
                pr.state = "merged"

    def lease(self, subject: str) -> Any:
        return next(le for le in self.state.leases if le.pr == subject)

    def result(self, subject: str, kind: str, **fields: Any) -> int:
        lease_id = int(self.lease(subject).lease_id)
        self.results.append(
            {"lease_id": lease_id, "pr": subject, "kind": kind, **fields}
        )
        return lease_id

    def exit(self, subject: str) -> None:
        self.alive[self.lease(subject).lease_id] = False

    def cause(self, key: str) -> Any:
        return next(c for c in self.state.causes if c.key == key)


def is_cause_subject(subject: str) -> bool:
    return subject.startswith("cause:")


def dispatched(decision: ModelLandingDecision) -> list[str]:
    return [a.subject for a in decision.actions if a.kind.value == "dispatch_worker"]


def cause_dispatched(decision: ModelLandingDecision) -> list[str]:
    return [s for s in dispatched(decision) if is_cause_subject(s)]


def per_pr_dispatched(decision: ModelLandingDecision) -> list[str]:
    return [s for s in dispatched(decision) if not is_cause_subject(s)]


def actions_of(decision: ModelLandingDecision, kind: str) -> list[ModelLandingAction]:
    return [a for a in decision.actions if a.kind.value == kind]


def member_prs(action: ModelLandingAction) -> list[str]:
    assert action.cause_brief is not None
    return [m.pr for m in action.cause_brief.members]


def recorded(decision: ModelLandingDecision) -> list[str]:
    return sorted(
        f"{r.pr} {r.outcome.value} {r.reason.value}" for r in decision.recorded_outcomes
    )


def three() -> list[Pr]:
    return [Pr(1), Pr(2), Pr(3)]


# ------------------------------------------------------------------ signature
@pytest.mark.unit
def test_normalize_signature_replaces_what_varies_per_pr() -> None:
    from omnimarket.handlers.cause_signature import (
        UNREAD,
        normalize_annotation,
        normalize_signature,
    )

    a = (
        "companion OCC#12642 is still OPEN -- it must merge before this PR may "
        "merge -- poll deadline (1500s) reached after 1508s at 2026-10-03T20:36:30Z "
        "on 3a1beb08992004c653336cbb4bf8039d20364b58"
    )
    b = (
        "companion OCC#12574 is still   OPEN -- it must merge before this PR may "
        "merge -- poll deadline (1500s) reached after 1531s at 2026-10-03T19:01:18Z "
        "on 0ad953a7f00d"
    )
    assert normalize_annotation(a) == normalize_annotation(b)
    assert "#<n>" in normalize_annotation(a)
    assert "<ts>" in normalize_annotation(a)
    assert "<sha>" in normalize_annotation(a)
    check = "occ-preflight / eligibility"
    assert normalize_signature(check, a) == normalize_signature(check, b)
    assert re.fullmatch(r"[0-9a-f]{12}", normalize_signature(check, a))
    assert normalize_signature("other check", a) != normalize_signature(check, a)
    ids_one = "OMN-17427 [dod-x-ac1, dod-x-ac2]: bound test also passes"
    ids_two = "OMN-18000 [dod-y-ac3]: bound test also passes"
    assert normalize_signature(CHECK, ids_one) == normalize_signature(CHECK, ids_two)
    generic = "Process completed with exit code 1.\nreal failure line"
    assert normalize_annotation(generic) == "real failure line"
    assert normalize_signature(CHECK, None) == UNREAD
    assert normalize_signature(CHECK, "") == UNREAD
    assert normalize_signature(CHECK, "Process completed with exit code 2.") == UNREAD
    assert len(normalize_annotation("word " * 200)) == 300


# --------------------------------------------------------------------- replay
def _replay_facts() -> ModelLandingFacts:
    doc = json.loads(FIXTURE.read_text())
    return ModelLandingFacts.model_validate(doc["facts"])


def _members_of_only_cause(decision: ModelLandingDecision) -> tuple[str, list[str]]:
    causes = [
        a
        for a in actions_of(decision, "dispatch_worker")
        if is_cause_subject(a.subject)
    ]
    assert len(causes) == 1, [a.subject for a in causes]
    return causes[0].subject, member_prs(causes[0])


@pytest.mark.unit
def test_replay_tick722_one_omnimarket_cause_covers_the_cluster() -> None:
    """AC1: one cause, at least 16 members, #3321 inside, no per-PR dispatch for a member."""
    decision = decide_landing(_replay_facts())
    key, members = _members_of_only_cause(decision)
    assert key.startswith(f"cause:{OMNIMARKET}:")
    assert len(members) >= 16
    assert f"{OMNIMARKET}#3321" in members
    assert not set(per_pr_dispatched(decision)) & set(members)
    # pinned by this replay: the dod-verify always-pass pair leads, 19 members
    assert key == "cause:OmniNode-ai/omnimarket:52c59723e1ad"
    assert len(members) == 19


@pytest.mark.unit
def test_replay_tick722_cause_dispatch_comes_first_with_one_bounded_lease() -> None:
    facts = _replay_facts()
    decision = decide_landing(facts)
    key, members = _members_of_only_cause(decision)
    dispatches = dispatched(decision)
    assert dispatches[0] == key
    leases = [le for le in decision.next_state.leases if le.pr == key]
    assert len(leases) == 1
    assert leases[0].deadline_at - facts.observed_at == timedelta(seconds=5400)
    assert leases[0].engine.value == "claude_opus"
    brief = decision.actions[
        [a.subject for a in decision.actions].index(key)
    ].cause_brief
    assert brief is not None
    assert brief.pairs[0].check == CHECK
    assert {p.check for p in brief.pairs} >= {CHECK, "OCC Preflight Dependency"}
    # the live per-PR lease on a member runs to its own deadline
    assert any(le.pr == f"{OMNIMARKET}#3338" for le in decision.next_state.leases)
    assert f"{OMNIMARKET}#3338" in members
    # a gate younger than the stall window stays out of the cause
    assert f"{OMNIMARKET}#3340" not in members


@pytest.mark.unit
def test_replay_tick722_lower_bound_gate_still_forms_a_cause() -> None:
    """With every gate dated at the tick tail's first sighting (20:36:14Z), only
    the PRs with no gate cluster: still one cause, holding #3321."""
    facts = _replay_facts()
    first_seen = datetime(2026, 10, 3, 20, 36, 14, tzinfo=UTC)
    prs = tuple(
        p.model_copy(update={"gate_since": first_seen}) if p.gate_since else p
        for p in facts.prs
    )
    decision = decide_landing(facts.model_copy(update={"prs": prs}))
    _, members = _members_of_only_cause(decision)
    assert f"{OMNIMARKET}#3321" in members
    assert all(not next(p for p in prs if p.pr == m).suspensions for m in members)
    assert not set(per_pr_dispatched(decision)) & set(members)


@pytest.mark.unit
def test_replay_tick722_fixture_carries_no_private_paths_or_handles() -> None:
    text = FIXTURE.read_text()
    assert not re.search(r"/(Users|home|root)/", text)
    assert not re.search(r"\bh20\d\b", text)
    prefixes = set(re.findall(r"\b([A-Za-z0-9_.-]+)/[A-Za-z0-9_.-]+", text))
    assert prefixes <= {"OmniNode-ai", "evidence", "operator", "test"}, prefixes


# ---------------------------------------------------------------- permutation
def _shuffled(items: tuple[Any, ...], how: str) -> tuple[Any, ...]:
    out = list(items)
    if how == "reversed":
        out.reverse()
    elif how == "rotated":
        out = out[len(out) // 2 :] + out[: len(out) // 2]
    elif how.startswith("shuffle"):
        random.Random(int(how.split("-")[1])).shuffle(out)
    return tuple(out)


def _permuted(facts: ModelLandingFacts, how: str) -> ModelLandingFacts:
    state = facts.state
    prs = tuple(
        p.model_copy(
            update={
                "red_annotations": dict(reversed(list(p.red_annotations.items()))),
                "red_checks": tuple(reversed(p.red_checks)),
            }
        )
        for p in facts.prs
    )
    return facts.model_copy(
        update={
            "prs": _shuffled(prs, how),
            "probes": _shuffled(facts.probes, how),
            "results": _shuffled(facts.results, how),
            "cause_owners": _shuffled(facts.cause_owners, how),
            "cause_releases": _shuffled(facts.cause_releases, how),
            "fixer_hold": _shuffled(facts.fixer_hold, how),
            "floor_breached_repos": _shuffled(facts.floor_breached_repos, how),
            "state": state.model_copy(
                update={
                    "leases": _shuffled(state.leases, how),
                    "records": _shuffled(state.records, how),
                    "causes": _shuffled(state.causes, how),
                }
            ),
        }
    )


def _midway_facts() -> ModelLandingFacts:
    """A two-repo world one tick after its causes dispatched, a result pending."""
    world = CauseWorld(
        [
            *three(),
            Pr(4),
            Pr(1, repo=OTHER),
            Pr(2, repo=OTHER),
            Pr(7, annotations={CHECK: "x"}),
        ],
        floor_breached_repos=[OTHER],
    )
    world.step()
    world.result(key_for(), "cause_not_shared", reason="coincidence")
    world.tick += 1
    world.now += TICK
    return world.facts()


PERMUTATIONS = ["reversed", "rotated", "shuffle-1", "shuffle-2", "shuffle-3"]


@pytest.mark.unit
@pytest.mark.parametrize("how", PERMUTATIONS)
@pytest.mark.parametrize("source", ["replay", "midway"])
def test_cause_permutation(source: str, how: str) -> None:
    """AC2: any permutation of the facts gives a byte-identical decision."""
    facts = _replay_facts() if source == "replay" else _midway_facts()
    first = decide_landing(facts).model_dump_json()
    assert decide_landing(_permuted(facts, how)).model_dump_json() == first
    assert (
        decide_landing(
            ModelLandingFacts.model_validate_json(facts.model_dump_json())
        ).model_dump_json()
        == first
    )


# ------------------------------------------------------------------ scenarios
@pytest.mark.unit
def test_cause_scenario_s21_cluster_of_three_gets_one_lease_and_no_per_pr_dispatch() -> (
    None
):
    world = CauseWorld(three())
    decision = world.step()
    key = key_for()
    assert dispatched(decision) == [key]
    (action,) = decision.actions
    assert action.cause_brief is not None
    assert action.cause_brief.brief_class.value == "shared_cause"
    assert action.cause_brief.attempt == 1
    assert action.cause_brief.engine.value == "claude_opus"
    assert member_prs(action) == [f"{REPO}#1", f"{REPO}#2", f"{REPO}#3"]
    lease = world.lease(key)
    assert lease.deadline_at - lease.dispatched_at == timedelta(seconds=5400)
    assert world.step().actions == ()  # members stay suppressed under the lease
    # two red PRs do not cluster: each gets its own worker, as before
    pair = CauseWorld([Pr(1), Pr(2)])
    assert sorted(dispatched(pair.step())) == [f"{REPO}#1", f"{REPO}#2"]


@pytest.mark.unit
def test_cause_scenario_s22_fix_merges_members_rerun_and_go_green() -> None:
    fix = Pr(99, ci="pending", red_checks=(), annotations={}, process_fix=True)
    world = CauseWorld([*three(), fix])
    key = key_for()
    assert dispatched(world.step()) == [key]
    world.result(key, "cause_fix_submitted", fix_ref=fix.key, fix_head=fix.head_sha)
    decision = world.step()
    assert recorded(decision) == [f"{key} cause_fix_submitted none"]
    rec = world.cause(key)
    assert (rec.attempts, rec.fix_ref) == (1, fix.key)
    assert per_pr_dispatched(decision) == []
    fix.ci = "green"
    decision = world.step()  # the controller lands the fix PR like any PR
    assert [a.subject for a in actions_of(decision, "merge")] == [fix.key]
    assert per_pr_dispatched(decision) == []
    decision = world.step()  # merged: one rerun per member head
    reruns = actions_of(decision, "rerun")
    assert sorted((a.subject, a.head_sha) for a in reruns) == sorted(
        (p.key, p.head_sha) for p in three()
    )
    assert actions_of(world.step(), "rerun") == []  # keyed by cause and head
    for n in (1, 2, 3):
        world.prs[f"{REPO}#{n}"].ci = "green"
    decision = world.step()
    assert sorted(a.subject for a in actions_of(decision, "merge")) == [
        f"{REPO}#1",
        f"{REPO}#2",
        f"{REPO}#3",
    ]
    assert world.state.causes == ()
    assert all(world.prs[f"{REPO}#{n}"].state == "merged" for n in (1, 2, 3))


@pytest.mark.unit
def test_cause_scenario_s23_two_failed_attempts_park_with_one_escalation_then_fallback() -> (
    None
):
    world = CauseWorld(three())
    key = key_for()
    world.step()
    world.result(
        key, "external_blocker", blocker_kind="upstream_open", blocker_ref="acme/lib#7"
    )
    assert recorded(world.step()) == [f"{key} external_blocker none"]
    assert actions_of(world.step(), "kill_worker")
    decision = world.step()  # released; the cause is still present: attempt 2
    assert dispatched(decision) == [key]
    assert decision.actions[0].cause_brief is not None
    assert decision.actions[0].cause_brief.attempt == 2
    world.step(advance=timedelta(seconds=5400))  # deadline with the process alive
    decision = world.step()
    assert f"{key} timed_out deadline" in recorded(decision)
    escalations = actions_of(decision, "escalate_operator")
    assert len(escalations) == 1
    rec = world.cause(key)
    assert rec.attempts == 2
    assert rec.parked_until == world.now + timedelta(hours=12)
    assert escalations[0].dedupe_key == f"{key}@{rec.parked_until.isoformat()}"
    assert [m.pr for m in escalations[0].members] == [p.key for p in three()]
    assert cause_dispatched(decision) == []
    assert sorted(per_pr_dispatched(decision)) == [p.key for p in three()]  # fallback
    assert [(d.reason.value, d.subject) for d in decision.degraded] == [
        ("cause_exhausted", key)
    ]
    for _ in range(3):  # the members' workers run; the park keeps its one MSG
        later = world.step()
        assert later.actions == ()
        assert [(d.reason.value, d.subject) for d in later.degraded] == [
            ("cause_exhausted", key)
        ]
    total = sum(len(actions_of(d, "escalate_operator")) for d in world.decisions)
    assert total == 1


@pytest.mark.unit
def test_cause_scenario_s24_fixer_hold_revokes_kills_and_confirms_with_no_dispatch() -> (
    None
):
    late = Pr(5, ci="pending", red_checks=(), annotations={})
    world = CauseWorld([*three(), late])
    key = key_for()
    world.step()
    lease_id = world.lease(key).lease_id
    world.extra["fixer_hold"] = [REPO]
    late.ci = "green"
    decision = world.step()
    assert [a.lease_id for a in actions_of(decision, "kill_worker")] == [lease_id]
    assert world.lease(key).revoked
    assert [a.subject for a in actions_of(decision, "merge")] == [late.key]
    assert dispatched(decision) == []
    decision = world.step()  # confirmed terminated: released, nothing dispatched
    assert recorded(decision) == [f"{key} timed_out revoked"]
    assert world.state.leases == ()
    assert dispatched(decision) == []
    assert world.cause(key).attempts == 0  # a revoke spends nothing
    assert dispatched(world.step()) == []
    world.extra["fixer_hold"] = []
    decision = world.step()
    assert dispatched(decision) == [key]
    assert decision.actions[0].cause_brief is not None
    assert decision.actions[0].cause_brief.attempt == 1


@pytest.mark.unit
def test_cause_scenario_s25_per_pr_claims_do_not_block_a_cause_claim_does() -> None:
    owned = ("owned",)
    prs = [
        Pr(1, suspensions=owned),
        Pr(2, suspensions=owned),
        Pr(3),
        green(4, suspensions=owned),
    ]
    world = CauseWorld(prs)
    decision = world.step()
    assert dispatched(decision) == [key_for()]
    assert member_prs(decision.actions[0]) == [f"{REPO}#1", f"{REPO}#2", f"{REPO}#3"]
    assert actions_of(decision, "merge") == []  # an owned PR is never merged here
    for owner in (
        {"lane": "cause-lane", "repo": REPO, "check": CHECK},
        {"lane": "cause-lane", "repo": REPO, "cause": key_for()},
    ):
        claimed = CauseWorld(
            [Pr(1, suspensions=owned), Pr(2, suspensions=owned), Pr(3)],
            cause_owners=[owner],
        )
        assert claimed.step().actions == ()  # no cause worker, no per-PR worker
    unrelated = CauseWorld(
        three(), cause_owners=[{"lane": "x", "repo": REPO, "check": "lint"}]
    )
    assert dispatched(unrelated.step()) == [key_for()]


@pytest.mark.unit
def test_cause_scenario_s26_an_escalated_pr_alone_becomes_a_cause() -> None:
    pr = Pr(1)
    world = CauseWorld([pr], records=[parked_record(pr)])
    decision = world.step()
    assert dispatched(decision) == [key_for()]
    assert member_prs(decision.actions[0]) == [pr.key]
    assert ("escalation_exhausted", pr.key) in [
        (d.reason.value, d.subject) for d in decision.degraded
    ]
    assert world.step().actions == ()


@pytest.mark.unit
def test_cause_scenario_s27_cause_not_shared_returns_members_to_per_pr_rung_zero() -> (
    None
):
    prs = three()
    world = CauseWorld(prs, records=[parked_record(prs[2])])
    key = key_for()
    assert dispatched(world.step()) == [key]
    world.result(
        key, "cause_not_shared", reason="the annotation matches by coincidence"
    )
    decision = world.step()
    assert recorded(decision) == [f"{key} cause_not_shared none"]
    per_pr = actions_of(decision, "dispatch_worker")
    assert sorted(a.subject for a in per_pr) == [p.key for p in prs]
    for action in per_pr:
        assert action.brief is not None
        assert action.brief.engine.value == "claude_sonnet"
        assert action.brief.brief_class.value == "real_red"
    records = {r.pr: r for r in world.state.records}
    assert all(records[p.key].ladder_index == 0 for p in prs)
    assert records[prs[2].key].parked_head is None
    for _ in range(3):  # blocked for this member set: no cause again
        assert not [s for s in dispatched(world.step()) if is_cause_subject(s)]
    prs[0].head += 1  # a member's head moves
    decision = world.step()
    assert key in dispatched(decision)


@pytest.mark.unit
def test_cause_scenario_s28_crash_between_state_write_and_spawn_leaves_one_lease() -> (
    None
):
    world = CauseWorld(three())
    key = key_for()
    world.crash_spawn = True
    world.step()  # next state written with the lease, the spawn never happened
    first = world.lease(key).lease_id
    world.crash_spawn = False
    decision = world.step()
    assert recorded(decision) == [f"{key} timed_out exited"]
    assert [le.pr for le in world.state.leases] == [key]
    assert world.lease(key).lease_id == first + 1
    rec = world.cause(key)
    assert (rec.attempts, rec.spawn_failures) == (0, 1)
    assert world.step().actions == ()
    assert [le.pr for le in world.state.leases] == [key]


# --------------------------------------------------------------------- budget
def _park_by_spawn_failures(world: CauseWorld, key: str) -> ModelLandingDecision:
    world.crash_spawn = True
    world.step()
    world.step()
    world.step()
    world.crash_spawn = False  # the park tick's own workers (the fallback) do spawn
    return world.step()


@pytest.mark.unit
def test_budget_three_spawn_failures_park_the_cause_and_members_fall_back() -> None:
    world = CauseWorld(three())
    key = key_for()
    decision = _park_by_spawn_failures(world, key)
    rec = world.cause(key)
    assert (rec.attempts, rec.spawn_failures) == (0, 3)
    assert len(actions_of(decision, "escalate_operator")) == 1
    assert cause_dispatched(decision) == []
    assert sorted(per_pr_dispatched(decision)) == [p.key for p in three()]  # fallback
    assert world.step().actions == ()  # parked: no cause worker, members working


@pytest.mark.unit
def test_budget_an_exit_after_the_spawn_window_spends_an_attempt() -> None:
    world = CauseWorld(three())
    key = key_for()
    world.step()
    world.exit(key)
    decision = world.step(advance=timedelta(seconds=900))
    assert recorded(decision) == [f"{key} timed_out exited"]
    assert decision.actions[0].cause_brief is not None
    assert decision.actions[0].cause_brief.attempt == 2
    assert world.cause(key).spawn_failures == 0


@pytest.mark.unit
def test_budget_an_unverified_fix_claim_spends_the_attempt() -> None:
    world = CauseWorld(three())
    key = key_for()
    world.step()
    world.result(key, "cause_fix_submitted", fix_ref=f"{REPO}#404", fix_head="a" * 40)
    decision = world.step()
    assert recorded(decision) == [f"{key} invalid unverified_claim"]
    assert [v.pr for v in decision.violations] == [key]
    assert world.cause(key).attempts == 1
    assert world.cause(key).fix_ref is None


@pytest.mark.unit
def test_budget_release_row_ends_the_park_early_and_a_stale_one_does_not() -> None:
    world = CauseWorld(three())
    key = key_for()
    _park_by_spawn_failures(world, key)
    world.extra["cause_releases"] = [
        {"cause": key, "at": (world.now - timedelta(hours=1)).isoformat()}
    ]
    assert dispatched(world.step()) == []
    world.extra["cause_releases"] = [{"cause": key, "at": world.now.isoformat()}]
    decision = world.step()
    assert dispatched(decision) == [key]
    assert decision.actions[0].cause_brief is not None
    assert decision.actions[0].cause_brief.attempt == 1


@pytest.mark.unit
def test_budget_park_ends_after_twelve_hours() -> None:
    world = CauseWorld(three())
    key = key_for()
    _park_by_spawn_failures(world, key)
    assert dispatched(world.step(advance=timedelta(hours=11))) == []
    assert dispatched(world.step(advance=timedelta(hours=1))) == [key]


# ----------------------------------------------------------------- fixer hold
@pytest.mark.unit
def test_fixer_hold_repo_scope_revokes_every_lease_there_and_dispatches_nothing() -> (
    None
):
    lone = Pr(7, annotations={CHECK: "a failure of its own"})
    elsewhere = Pr(1, repo=OTHER)
    world = CauseWorld([*three(), lone, elsewhere])
    first = world.step()
    assert sorted(dispatched(first)) == sorted([key_for(), lone.key, elsewhere.key])
    world.extra["fixer_hold"] = [REPO]
    decision = world.step()
    killed = {a.subject for a in actions_of(decision, "kill_worker")}
    assert killed == {key_for(), lone.key}
    decision = world.step()
    assert {le.pr for le in world.state.leases} == {elsewhere.key}
    assert dispatched(decision) == []
    world.extra["fixer_hold"] = []
    assert sorted(dispatched(world.step())) == sorted([key_for(), lone.key])


@pytest.mark.unit
def test_fixer_hold_all_scope_still_merges_and_reruns() -> None:
    cascade = Pr(8, red_class="cascade", annotations={})
    world = CauseWorld([*three(), green(9), cascade], fixer_hold=["all"])
    decision = world.step()
    assert dispatched(decision) == []
    assert [a.subject for a in actions_of(decision, "merge")] == [f"{REPO}#9"]
    assert [a.subject for a in actions_of(decision, "rerun")] == [cascade.key]


# -------------------------------------------------------- escalated singleton
@pytest.mark.unit
def test_escalated_singleton_dispatches_one_cause_for_a_parked_pr() -> None:
    pr = Pr(1)
    world = CauseWorld([pr, Pr(2, repo=OTHER)], records=[parked_record(pr)])
    decision = world.step()
    assert key_for() in dispatched(decision)
    assert pr.key not in per_pr_dispatched(decision)
    assert len([s for s in dispatched(decision) if is_cause_subject(s)]) == 1


@pytest.mark.unit
def test_escalated_singleton_is_not_minted_for_a_pr_a_cluster_covers() -> None:
    prs = three()
    world = CauseWorld(prs, records=[parked_record(prs[0])])
    decision = world.step()
    assert dispatched(decision) == [key_for()]
    assert prs[0].key in member_prs(decision.actions[0])


@pytest.mark.unit
def test_escalated_singleton_needs_a_readable_annotation() -> None:
    pr = Pr(1, annotations={})
    world = CauseWorld([pr], records=[parked_record(pr)])
    decision = world.step()
    assert decision.actions == ()
    assert [(d.reason.value, d.subject) for d in decision.degraded] == [
        ("escalation_exhausted", pr.key)
    ]


# ------------------------------------------------------------------- clusters
@pytest.mark.unit
def test_gate_joins_clusters_only_after_the_stall_window() -> None:
    def gated(minutes: int) -> list[Pr]:
        since = EPOCH + TICK - timedelta(minutes=minutes)
        return [Pr(n, suspensions=("gate",), gate_since=since) for n in (1, 2, 3)]

    assert CauseWorld(gated(59)).step().actions == ()
    assert CauseWorld(gated(60)).step().actions == ()
    assert dispatched(CauseWorld(gated(61)).step()) == [key_for()]
    undated = [Pr(n, suspensions=("gate",)) for n in (1, 2, 3)]
    assert CauseWorld(undated).step().actions == ()


@pytest.mark.unit
@pytest.mark.parametrize("suspension", ["hold", "draft", "do_not_land"])
def test_person_holds_drafts_and_do_not_land_never_join(suspension: str) -> None:
    world = CauseWorld([Pr(1), Pr(2), Pr(3, suspensions=(suspension,))])
    decision = world.step()
    assert not [s for s in dispatched(decision) if is_cause_subject(s)]
    assert sorted(dispatched(decision)) == [f"{REPO}#1", f"{REPO}#2"]


@pytest.mark.unit
def test_follower_checks_reviewer_pool_and_unread_never_cluster() -> None:
    for check in ("CI Summary", "Hostile Review Gate"):
        prs = [Pr(n, red_checks=(check,), annotations={check: ANN}) for n in (1, 2, 3)]
        assert not [
            s for s in dispatched(CauseWorld(prs).step()) if is_cause_subject(s)
        ]
    unread = [Pr(n, annotations={}) for n in (1, 2, 3)]
    assert not [s for s in dispatched(CauseWorld(unread).step()) if is_cause_subject(s)]


@pytest.mark.unit
def test_a_rerunnable_red_reruns_before_it_clusters() -> None:
    prs = [Pr(n, red_class="cascade") for n in (1, 2, 3)]
    decision = CauseWorld(prs).step()
    assert dispatched(decision) == []
    assert sorted(a.subject for a in actions_of(decision, "rerun")) == [
        p.key for p in prs
    ]


@pytest.mark.unit
def test_an_absorbed_cluster_carries_its_pairs_into_one_cause() -> None:
    lead = "occ-preflight / eligibility"
    lead_text = "companion OCC#1 is still OPEN"
    prs = []
    for n in range(1, 6):
        checks = {lead: lead_text}
        if n <= 4:
            checks[CHECK] = ANN
        prs.append(Pr(n, red_checks=tuple(checks), annotations=checks))
    decision = CauseWorld(prs).step()
    (action,) = actions_of(decision, "dispatch_worker")
    assert action.subject == key_for(lead, lead_text)
    assert action.cause_brief is not None
    assert [p.check for p in action.cause_brief.pairs] == [lead, CHECK]
    assert len(member_prs(action)) == 5


@pytest.mark.unit
def test_a_disjoint_cluster_is_its_own_cause() -> None:
    other = "lint"
    prs = [*three()] + [
        Pr(n, red_checks=(other,), annotations={other: "E501 line too long"})
        for n in (4, 5, 6)
    ]
    decision = CauseWorld(prs).step()
    assert sorted(dispatched(decision)) == sorted(
        [key_for(), key_for(other, "E501 line too long")]
    )


@pytest.mark.unit
def test_cause_caps_four_fleet_wide_two_per_repo() -> None:
    prs: list[Pr] = []
    for repo in ("acme/a", "acme/b", "acme/c"):
        for c in range(3):
            check = f"check-{c}"
            prs += [
                Pr(
                    10 * c + n,
                    repo=repo,
                    red_checks=(check,),
                    annotations={check: "boom"},
                )
                for n in (1, 2, 3)
            ]
    decision = CauseWorld(prs).step()
    causes = [s for s in dispatched(decision) if is_cause_subject(s)]
    assert len(causes) == 4
    per_repo: dict[str, int] = {}
    for key in causes:
        repo = key.split(":")[1]
        per_repo[repo] = per_repo.get(repo, 0) + 1
    assert max(per_repo.values()) == 2
    leases_per_repo: dict[str, int] = {}
    for lease in decision.next_state.leases:
        repo = (
            lease.pr.split(":")[1]
            if is_cause_subject(lease.pr)
            else lease.pr.split("#")[0]
        )
        leases_per_repo[repo] = leases_per_repo.get(repo, 0) + 1
    assert max(leases_per_repo.values()) <= 6


@pytest.mark.unit
def test_per_repo_cap_six_workers_of_any_kind() -> None:
    prs = [Pr(n, annotations={CHECK: f"failure number {'x' * n}"}) for n in range(1, 9)]
    decision = CauseWorld(prs).step()
    assert len(dispatched(decision)) == 6


@pytest.mark.unit
def test_floor_breached_repo_clusters_at_two_and_dispatches_first() -> None:
    floor = [Pr(1, repo=OTHER), Pr(2, repo=OTHER)]
    big = [Pr(n) for n in range(1, 6)]
    world = CauseWorld(
        [*floor, *big], policy={"max_cause_workers": 1}, floor_breached_repos=[OTHER]
    )
    decision = world.step()
    causes = [s for s in dispatched(decision) if is_cause_subject(s)]
    assert causes == [key_for(repo=OTHER)]


@pytest.mark.unit
def test_cause_dispatch_precedes_per_pr_dispatch_in_a_tick() -> None:
    lone = Pr(1, repo=OTHER, annotations={CHECK: "unrelated"})
    decision = CauseWorld([*three(), lone]).step()
    assert dispatched(decision) == [key_for(), lone.key]


@pytest.mark.unit
def test_cause_brief_text_and_rules_are_fixed() -> None:
    from pydantic import ValidationError

    from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
        BRIEF_INSTRUCTIONS,
        ModelLandingCauseBrief,
    )

    decision = CauseWorld(three()).step()
    brief = decision.actions[0].cause_brief
    assert brief is not None
    data = brief.model_dump()
    assert data["instructions"] == BRIEF_INSTRUCTIONS[brief.brief_class]
    assert [k.value for k in brief.allowed_results] == [
        "cause_fix_submitted",
        "cause_not_shared",
        "external_blocker",
    ]
    with pytest.raises(ValidationError):
        ModelLandingCauseBrief(
            **{**data, "instructions": data["instructions"] + " and more"}
        )
    with pytest.raises(ValidationError):
        ModelLandingCauseBrief(**{**data, "push_rule": "push to every member branch"})


# ------------------------------------------- T1 model findings LC-F2 and LC-F3
# omnibase_internal#144 (tla/landing_controller, S21 to S28 reached, every
# mutant caught) found two gaps in the plan's section 2.4. LC-F2 (mutant
# M_P11_late_exit_uncounted): an exit with no result after the spawn window and
# before the deadline counted against no budget, so a cause whose worker keeps
# dying was redispatched without bound. LC-F3 (mutant M_P14_sidecar_dedupe): a
# dedupe key kept only in the controller's state escalated twice after a crash
# between the operator MSG and the state write. The ledger row is the source.
def _exit_mid_run(
    world: CauseWorld, key: str, after: int = 900
) -> ModelLandingDecision:
    world.exit(key)
    return world.step(advance=timedelta(seconds=after))


@pytest.mark.unit
def test_lc_f2_a_worker_that_keeps_dying_mid_run_parks_after_two_attempts() -> None:
    world = CauseWorld(three())
    key = key_for()
    world.step()
    first = _exit_mid_run(world, key)
    assert recorded(first) == [f"{key} timed_out exited"]
    assert dispatched(first) == [key]
    second = _exit_mid_run(world, key)
    assert recorded(second) == [f"{key} timed_out exited"]
    assert cause_dispatched(second) == []
    assert len(actions_of(second, "escalate_operator")) == 1
    rec = world.cause(key)
    assert (rec.attempts, rec.spawn_failures) == (2, 0)
    assert rec.parked_until == world.now + timedelta(hours=12)
    for _ in range(4):
        later = world.step()
        assert cause_dispatched(later) == []
        assert actions_of(later, "escalate_operator") == []
    total = sum(len(actions_of(d, "escalate_operator")) for d in world.decisions)
    assert total == 1


@pytest.mark.unit
def test_lc_f2_the_spawn_window_edge_splits_a_spawn_failure_from_an_attempt() -> None:
    inside = CauseWorld(three())
    key = key_for()
    inside.step()
    _exit_mid_run(inside, key, after=599)
    assert (inside.cause(key).attempts, inside.cause(key).spawn_failures) == (0, 1)

    at_edge = CauseWorld(three())
    at_edge.step()
    _exit_mid_run(at_edge, key, after=600)
    assert (at_edge.cause(key).attempts, at_edge.cause(key).spawn_failures) == (1, 0)


@pytest.mark.unit
def test_lc_f2_exits_before_and_after_the_window_each_count_toward_one_budget() -> None:
    world = CauseWorld(three())
    key = key_for()
    world.step()
    _exit_mid_run(world, key, after=300)  # a spawn failure, not an attempt
    assert (world.cause(key).attempts, world.cause(key).spawn_failures) == (0, 1)
    _exit_mid_run(world, key, after=1800)  # a late exit: an attempt
    assert (world.cause(key).attempts, world.cause(key).spawn_failures) == (1, 1)
    decision = _exit_mid_run(world, key, after=1800)  # the second attempt parks it
    assert (world.cause(key).attempts, world.cause(key).spawn_failures) == (2, 1)
    assert len(actions_of(decision, "escalate_operator")) == 1
    assert cause_dispatched(decision) == []


def _park_tick(world: CauseWorld, key: str) -> tuple[ModelLandingControllerState, Any]:
    before = world.state
    world.exit(key)
    decision = world.step(advance=timedelta(seconds=900))
    escalations = actions_of(decision, "escalate_operator")
    assert len(escalations) == 1
    return before, escalations[0]


def _ledger_row(world: CauseWorld, key: str, action: Any) -> dict[str, Any]:
    return {
        "cause": key,
        "at": world.now.isoformat(),
        "dedupe_key": action.dedupe_key,
    }


@pytest.mark.unit
def test_lc_f3_a_crash_between_the_msg_and_the_state_write_escalates_once() -> None:
    world = CauseWorld(three())
    key = key_for()
    world.step()
    _exit_mid_run(world, key)
    before, action = _park_tick(world, key)
    row = _ledger_row(world, key, action)
    world.state = before  # the controller died before it wrote the next state
    world.extra["cause_escalations"] = [row]
    decision = world.step()
    assert actions_of(decision, "escalate_operator") == []
    assert cause_dispatched(decision) == []
    rec = world.cause(key)
    assert rec.parked_until == datetime.fromisoformat(row["at"]) + timedelta(hours=12)
    assert rec.escalated == row["dedupe_key"]
    total = sum(len(actions_of(d, "escalate_operator")) for d in world.decisions)
    assert total == 1  # the model's P14: one MSG for the park episode


@pytest.mark.unit
def test_lc_f3_without_a_ledger_row_the_same_crash_still_escalates() -> None:
    """Positive control: the row, and only the row, suppresses the second MSG."""
    world = CauseWorld(three())
    key = key_for()
    world.step()
    _exit_mid_run(world, key)
    before, _ = _park_tick(world, key)
    world.state = before
    decision = world.step()
    assert len(actions_of(decision, "escalate_operator")) == 1


@pytest.mark.unit
def test_lc_f3_two_crashes_in_a_row_still_escalate_once() -> None:
    world = CauseWorld(three())
    key = key_for()
    world.step()
    _exit_mid_run(world, key)
    before, action = _park_tick(world, key)
    world.extra["cause_escalations"] = [_ledger_row(world, key, action)]
    for _ in range(2):
        world.state = before
        assert actions_of(world.step(), "escalate_operator") == []
    assert world.cause(key).escalated == action.dedupe_key


@pytest.mark.unit
def test_lc_f3_a_row_older_than_the_park_does_not_cover_a_new_episode() -> None:
    world = CauseWorld(three())
    key = key_for()
    world.step()
    _exit_mid_run(world, key)
    before, action = _park_tick(world, key)
    stale = _ledger_row(world, key, action)
    stale["at"] = (world.now - timedelta(hours=13)).isoformat()
    world.state = before
    world.extra["cause_escalations"] = [stale]
    assert len(actions_of(world.step(), "escalate_operator")) == 1


@pytest.mark.unit
def test_lc_f3_a_release_after_the_row_starts_a_new_episode() -> None:
    world = CauseWorld(three())
    key = key_for()
    world.step()
    _exit_mid_run(world, key)
    before, action = _park_tick(world, key)
    row = _ledger_row(world, key, action)
    world.state = before
    world.extra["cause_escalations"] = [row]
    world.extra["cause_releases"] = [
        {"cause": key, "at": (world.now + timedelta(minutes=1)).isoformat()}
    ]
    assert (
        len(actions_of(world.step(advance=timedelta(minutes=2)), "escalate_operator"))
        == 1
    )


@pytest.mark.unit
def test_lc_f3_rows_for_other_causes_and_their_order_change_nothing() -> None:
    world = CauseWorld(three())
    key = key_for()
    other = key_for(check="build / unit", text="boom")
    world.step()
    _exit_mid_run(world, key)
    before, action = _park_tick(world, key)
    rows = [
        _ledger_row(world, key, action),
        {"cause": other, "at": world.now.isoformat(), "dedupe_key": f"{other}@x"},
    ]
    world.state = before
    world.extra["cause_escalations"] = rows
    first = decide_landing(world.facts()).model_dump_json()
    world.extra["cause_escalations"] = list(reversed(rows))
    assert decide_landing(world.facts()).model_dump_json() == first
    assert actions_of(world.step(), "escalate_operator") == []


# ------------------------------------------------ parked cause: per-PR fallback
# Operator ruling: a parked cause no longer holds its members. Red PRs keep
# getting fixed per PR while the cause waits out its park; a live cause lease,
# an open fix PR or an owner still hold them.
def _park_by_two_failed_attempts(world: CauseWorld, key: str) -> ModelLandingDecision:
    """The s23 path: two attempts that fail, the second at its deadline."""
    world.step()
    world.result(
        key, "external_blocker", blocker_kind="upstream_open", blocker_ref="acme/lib#7"
    )
    world.step()
    world.step()  # the kill
    world.step()  # released: attempt 2
    world.step(advance=timedelta(seconds=5400))  # deadline with the process alive
    return world.step()


def _escalations(world: CauseWorld) -> int:
    return sum(len(actions_of(d, "escalate_operator")) for d in world.decisions)


def _assert_fallback_once(
    world: CauseWorld, key: str, park: ModelLandingDecision
) -> None:
    members = [p.key for p in three()]
    assert len(actions_of(park, "escalate_operator")) == 1
    assert [s for s in dispatched(park) if is_cause_subject(s)] == []
    assert sorted(per_pr_dispatched(park)) == members
    for action in actions_of(park, "dispatch_worker"):
        assert action.brief is not None
        assert action.brief.engine.value == "claude_sonnet"
        assert action.brief.brief_class.value == "real_red"
    assert world.cause(key).parked_until is not None
    for _ in range(3):  # parked, members working: no cause worker, no second MSG
        later = world.step()
        assert dispatched(later) == []
        assert actions_of(later, "escalate_operator") == []
    assert _escalations(world) == 1


@pytest.mark.unit
def test_fallback_a_cause_parked_by_two_failed_attempts_releases_its_members() -> None:
    world = CauseWorld(three())
    key = key_for()
    park = _park_by_two_failed_attempts(world, key)
    assert world.cause(key).attempts == 2
    _assert_fallback_once(world, key, park)


@pytest.mark.unit
def test_fallback_a_cause_parked_by_spawn_failures_releases_its_members() -> None:
    world = CauseWorld(three())
    key = key_for()
    park = _park_by_spawn_failures(world, key)
    assert (world.cause(key).attempts, world.cause(key).spawn_failures) == (0, 3)
    _assert_fallback_once(world, key, park)


@pytest.mark.unit
def test_fallback_positive_control_a_live_cause_lease_still_holds_its_members() -> None:
    world = CauseWorld(three())
    key = key_for()
    assert dispatched(world.step()) == [key]
    for _ in range(4):
        decision = world.step()
        assert world.lease(key).revoked is False
        assert per_pr_dispatched(decision) == []
        assert decision.actions == ()
    assert world.cause(key).parked_until is None


def _parked_world(
    key: str, policy: dict[str, Any] | None = None, prs: list[Pr] | None = None
) -> CauseWorld:
    """A fresh world carrying a parked cause and no worker: the park, then its members."""
    parked = CauseWorld(prs or three(), policy=policy)
    _park_by_spawn_failures(parked, key)
    parked.state = parked.state.model_copy(update={"leases": ()})
    fresh = CauseWorld(prs or three(), policy=policy)
    fresh.state = ModelLandingControllerState.model_validate(
        {"causes": [c.model_dump(mode="json") for c in parked.state.causes]}
    )
    fresh.now = parked.now
    return fresh


@pytest.mark.unit
def test_fallback_a_parked_cause_with_an_owner_still_holds_its_members() -> None:
    key = key_for()
    world = _parked_world(key)
    world.extra["cause_owners"] = [{"lane": "cause-lane", "repo": REPO, "cause": key}]
    decision = world.step()
    assert decision.actions == ()
    assert world.cause(key).parked_until is not None


@pytest.mark.unit
def test_fallback_a_parked_cause_without_an_owner_releases_members_at_once() -> None:
    key = key_for()
    world = _parked_world(key)
    decision = world.step()
    assert sorted(per_pr_dispatched(decision)) == [p.key for p in three()]
    assert [s for s in dispatched(decision) if is_cause_subject(s)] == []
    assert actions_of(decision, "escalate_operator") == []


@pytest.mark.unit
def test_fallback_a_release_row_ends_the_park_and_the_cause_holds_members_again() -> (
    None
):
    world = CauseWorld(three())
    key = key_for()
    park = _park_by_spawn_failures(world, key)
    workers = per_pr_dispatched(park)
    assert len(workers) == 3
    for pr in workers:  # the fallback workers died; their next rung would dispatch
        world.exit(pr)
    # the release row is newer than the park: the cause redispatches and holds
    world.extra["cause_releases"] = [
        {"cause": key, "at": (world.now + timedelta(seconds=900)).isoformat()}
    ]
    decision = world.step(advance=timedelta(seconds=900))
    assert dispatched(decision) == [key]
    assert per_pr_dispatched(decision) == []
    assert world.cause(key).parked_until is None
    assert decision.actions[0].cause_brief is not None
    assert decision.actions[0].cause_brief.attempt == 1


@pytest.mark.unit
def test_fallback_without_a_release_row_the_dead_fallback_workers_climb_the_ladder() -> (
    None
):
    """Control for the release test: the same exits, no row, members redispatch."""
    world = CauseWorld(three())
    key = key_for()
    workers = per_pr_dispatched(_park_by_spawn_failures(world, key))
    assert len(workers) == 3
    for pr in workers:
        world.exit(pr)
    decision = world.step(advance=timedelta(seconds=900))
    assert [s for s in dispatched(decision) if is_cause_subject(s)] == []
    assert sorted(per_pr_dispatched(decision)) == workers


@pytest.mark.unit
def test_fallback_per_pr_caps_bound_the_dispatches_of_a_big_cluster() -> None:
    members = [Pr(n) for n in range(1, 9)]
    key = key_for()
    world = _parked_world(key, prs=members)
    decision = world.step()
    assert len(per_pr_dispatched(decision)) == 6  # max_workers_per_repo


@pytest.mark.unit
def test_fallback_the_worker_pool_bounds_the_dispatches() -> None:
    key = key_for()
    world = _parked_world(key, policy={"max_workers": 2})
    assert len(per_pr_dispatched(world.step())) == 2


@pytest.mark.unit
def test_fallback_the_load_pause_stops_the_dispatches() -> None:
    key = key_for()
    world = _parked_world(key)
    world.extra["load1"] = 25.0
    assert dispatched(world.step()) == []
    world.extra["load1"] = 0.0
    assert len(per_pr_dispatched(world.step())) == 3


@pytest.mark.unit
def test_fallback_a_fixer_hold_dispatches_nothing_for_a_parked_cause() -> None:
    key = key_for()
    world = _parked_world(key)
    world.extra["fixer_hold"] = [REPO]
    assert dispatched(world.step()) == []
    world.extra["fixer_hold"] = []
    assert sorted(per_pr_dispatched(world.step())) == [p.key for p in three()]


@pytest.mark.unit
def test_fallback_a_parked_pr_stays_parked() -> None:
    """Per-PR parks (parked_head) are not part of the ruling."""
    prs = three()
    key = key_for()
    world = _parked_world(key, prs=prs)
    world.state = ModelLandingControllerState.model_validate(
        {
            "causes": [c.model_dump(mode="json") for c in world.state.causes],
            "records": [parked_record(prs[0])],
        }
    )
    decision = world.step()
    assert sorted(per_pr_dispatched(decision)) == [prs[1].key, prs[2].key]
