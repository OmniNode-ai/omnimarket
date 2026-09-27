# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Safety properties of the landing reducer over seeded random walks (OMN-19828, T6).

Section 4 of revision 1 of plan 5.1 states the properties the model checked;
the reducer tests assert them. Each walk drives the real handler from first
sight through observations drawn from every kind the table reads: fresh and
reordered pushes, same-head snapshots newer and older by the ordering key,
companion outcomes for the command in flight and for stale commands, verdicts
of every class with run attempts, arm answers, fresh and stale bound expiries,
close, reopen and merge. Between steps a dispatcher may drain the outbox head
(F8). Every step is checked against:

* P1     no arm or enqueue of a draft or held PR, and each carries its head;
* P1arm  a draft or held row is not armed and has no unsent arm (settled form);
* P1new  a new head disarms an armed row, and no unsent arm names an old head;
* P2     no re-run on product_failed, at most one re-run per (head, check);
* P3     at most one companion command in flight, each id issued once;
* P4     at most one terminal per (PR, episode), nothing after merged;
* CompTracked  a pending companion names the one command in flight, unanswered;
* TimerFresh   an expiry applies only to its own state entry, never in PARKED,
               and the generation moves exactly when state, head or episode do;
* P7     entering a waiting state leaves something in flight to move it.

The walks are seeded, so a failure reproduces from its seed and step.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingArmMethod,
    EnumPrLandingCompanionOutcome,
    EnumPrLandingCompanionStatus,
    EnumPrLandingIntentKind,
    EnumPrLandingObservationKind,
    EnumPrLandingState,
    ModelPrLandingCheckAttempt,
    ModelPrLandingObservation,
    ModelPrLandingState,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers import (
    CLOSED_EPISODE_TRIGGERS,
    HandlerPrLandingReducer,
)
from omnimarket.nodes.node_pr_landing_reducer.models import (
    ModelPrLandingReduceInput,
    ModelPrLandingReduceOutput,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    HEAD_CHECK_RERUN_VERDICTS,
    EnumHeadCheckVerdict,
)
from tests.unit.nodes.node_pr_landing_reducer._builders import (
    FIXTURES,
    OCC_PR,
    PR_NUMBER,
    REPOSITORY,
    T0,
    TICKETS,
    head,
    observation,
    start_row,
)

pytestmark = pytest.mark.unit

_K = EnumPrLandingObservationKind
_S = EnumPrLandingState
_I = EnumPrLandingIntentKind
_ARMS = frozenset({_I.GITHUB_ARM, _I.GITHUB_ENQUEUE})
_COMMANDS = frozenset({_I.COMPANION_DERIVE, _I.COMPANION_REGENERATE})
_WAITING = frozenset(
    {_S.COMPANION_PENDING, _S.COMPANION_OPEN, _S.CHECKS_PENDING, _S.READY}
)
_TERMINAL = frozenset({_S.MERGED, _S.CLOSED})
_CHECKS = ("ci", "lint", "change-control")

WALKS = 600
STEPS = 50


@dataclass
class _Walk:
    """What the walk has issued and seen, for the cross-step properties."""

    rng: random.Random
    heads_seen: int = 1
    next_seq: int = 1
    event: int = 0
    reruns: set[tuple[str, str]] = field(default_factory=set)
    commands: set[str] = field(default_factory=set)
    answered: set[str] = field(default_factory=set)
    terminals: dict[int, int] = field(default_factory=dict)


def _obs(
    walk: _Walk, kind: EnumPrLandingObservationKind, **fields: Any
) -> ModelPrLandingObservation:
    walk.event += 1
    return ModelPrLandingObservation.model_validate(
        {
            "repository": REPOSITORY,
            "pr_number": PR_NUMBER,
            "kind": kind,
            "observed_at": T0 + timedelta(seconds=walk.event),
            "source_topic": "test.pr-landing",
            "source_event_id": f"e{walk.event}",
            **fields,
        }
    )


def _newer(walk: _Walk) -> int:
    walk.next_seq += 1
    return walk.next_seq


def _older(walk: _Walk, row: ModelPrLandingState) -> int:
    return walk.rng.randint(1, max(1, row.source_seq))


def _flags(walk: _Walk) -> dict[str, bool]:
    rng = walk.rng
    return {"draft": rng.random() < 0.2, "held": rng.random() < 0.15}


def _snapshot(walk: _Walk, row: ModelPrLandingState) -> ModelPrLandingObservation:
    rng = walk.rng
    kind = rng.choice(
        [
            _K.READY_FOR_REVIEW,
            _K.CONVERTED_TO_DRAFT,
            _K.HOLD_APPLIED,
            _K.HOLD_LIFTED,
            _K.TITLE_EDITED,
            _K.REOPENED,
        ]
    )
    seq = _newer(walk) if rng.random() < 0.75 else _older(walk, row)
    fields: dict[str, Any] = {"head_sha": row.head_sha, "source_seq": seq}
    if rng.random() < 0.5:
        flags = _flags(walk)
        if kind is _K.CONVERTED_TO_DRAFT:
            flags["draft"] = True
        if kind is _K.READY_FOR_REVIEW:
            flags["draft"] = False
        if kind is _K.HOLD_APPLIED:
            flags["held"] = True
        if kind is _K.HOLD_LIFTED:
            flags["held"] = False
        fields.update(flags)
    return _obs(walk, kind, **fields)


def _verdict(walk: _Walk, row: ModelPrLandingState) -> ModelPrLandingObservation:
    rng = walk.rng
    verdict = rng.choice(list(EnumHeadCheckVerdict))
    names = rng.sample(_CHECKS, rng.randint(1, 2))
    at_head = row.head_sha
    if walk.heads_seen > 1 and rng.random() < 0.2:
        at_head = head(f"h{rng.randint(1, walk.heads_seen)}")
    return _obs(
        walk,
        _K.HEAD_CHECKS,
        head_sha=at_head,
        verdict=verdict,
        rerun_checks=tuple(names) if verdict in HEAD_CHECK_RERUN_VERDICTS else (),
        check_attempts=tuple(
            ModelPrLandingCheckAttempt(check=n, attempt=rng.randint(1, 3))
            for n in names
        ),
        arm_method=rng.choice(
            [EnumPrLandingArmMethod.AUTO_MERGE, EnumPrLandingArmMethod.QUEUE]
        ),
    )


def _outcome(walk: _Walk, row: ModelPrLandingState) -> ModelPrLandingObservation:
    rng = walk.rng
    outcome = rng.choice(list(EnumPrLandingCompanionOutcome))
    command = row.companion.command_id
    if command is None or rng.random() < 0.25:
        command = rng.choice(sorted(walk.commands) or ["never-issued"])
    fields: dict[str, Any] = {"command_id": command, "companion_outcome": outcome}
    if outcome is EnumPrLandingCompanionOutcome.MINTED:
        fields |= {
            "occ_pr": OCC_PR,
            "companion_stamped": rng.random() < 0.5,
            "companion_armed": rng.random() < 0.5,
        }
    if outcome is EnumPrLandingCompanionOutcome.DECLINED:
        fields["detail"] = "no ticket evidence"
    return _obs(walk, _K.COMPANION_OUTCOME, **fields)


def _draw(walk: _Walk, row: ModelPrLandingState) -> ModelPrLandingObservation | None:
    """The next observation, or None for a dispatcher drain of the outbox head."""
    rng = walk.rng
    if row.state is _S.OBSERVED and row.head_sha is not None and rng.random() < 0.8:
        return _obs(
            walk,
            _K.EVALUATION,
            companion_required=rng.random() < 0.5,
            base_served=rng.random() < 0.95,
        )
    if row.state is _S.READY and rng.random() < 0.5:
        return _obs(walk, _K.ARMED_CONFIRMED, head_sha=row.head_sha)
    if row.state is _S.CHECKS_PENDING and rng.random() < 0.3:
        return _verdict(walk, row)
    roll = rng.random()
    if row.head_sha is None or roll < 0.08:
        walk.heads_seen += 1
        return _obs(
            walk,
            _K.PUSHED,
            head_sha=head(f"h{walk.heads_seen}"),
            source_seq=_newer(walk),
            ticket_ids=TICKETS,
            **_flags(walk),
        )
    if roll < 0.12:
        stale = head(f"h{rng.randint(1, walk.heads_seen)}")
        return _obs(walk, _K.PUSHED, head_sha=stale, source_seq=_older(walk, row))
    if roll < 0.30:
        return _snapshot(walk, row)
    if roll < 0.45:
        return _verdict(walk, row)
    if roll < 0.55:
        return _outcome(walk, row)
    if roll < 0.62:
        kind = rng.choice(
            [_K.COMPANION_MERGED, _K.COMPANION_CONFLICTING, _K.COMPANION_CLOSED]
        )
        return _obs(walk, kind, occ_pr=OCC_PR)
    if roll < 0.68:
        kind = rng.choice([_K.ARMED_CONFIRMED, _K.DISARMED])
        return _obs(walk, kind, head_sha=row.head_sha)
    if roll < 0.74:
        fresh = rng.random() < 0.6
        return _obs(
            walk,
            _K.BOUND_EXPIRED,
            episode=row.episode if fresh else max(0, row.episode - rng.randint(0, 1)),
            state_entry_generation=(
                row.state_entry_generation
                if fresh
                else max(0, row.state_entry_generation - rng.randint(1, 2))
            ),
        )
    if roll < 0.77:
        seq = _newer(walk) if rng.random() < 0.8 else _older(walk, row)
        return _obs(walk, _K.CLOSED, source_seq=seq)
    if roll < 0.78:
        return _obs(walk, _K.MERGED)
    return None


def _drain(row: ModelPrLandingState) -> ModelPrLandingState:
    if not row.outbox:
        return row
    return row.model_copy(update={"outbox": row.outbox[1:]})


def _product(
    kinds: frozenset[EnumPrLandingIntentKind], row: ModelPrLandingState
) -> list[Any]:
    return [i for i in row.outbox if i.target_pr is None and i.kind in kinds]


def _check(
    walk: _Walk,
    pre: ModelPrLandingState,
    obs: ModelPrLandingObservation,
    out: ModelPrLandingReduceOutput,
) -> None:
    row = out.state
    emitted = list(out.intents)
    kinds = [i.kind for i in emitted]

    if out.dropped_reason is not None:
        assert row == pre
        assert not emitted
        return
    assert pre.state is not _S.MERGED, "nothing applies after merged (P4)"
    assert row.seq == pre.seq + 1

    # P1: never arm or enqueue a draft or held PR; an arm carries the head it
    # was judged for, which is the head the green verdict read (the head-match
    # drop rule is what keeps an old head's verdict from arming a new head).
    for arm in (i for i in emitted if i.kind in _ARMS and i.target_pr is None):
        assert not row.draft
        assert not row.held
        assert not pre.draft
        assert not pre.held
        assert arm.head_sha == row.head_sha == obs.head_sha
        assert obs.verdict is EnumHeadCheckVerdict.GREEN
    # P1arm (settled form): a draft or held row is not armed, and nothing unsent arms it.
    if row.draft or row.held:
        assert row.armed is None
        assert not _product(_ARMS, row)
    # F6: never an unsent arm and an unsent disarm of the product PR together.
    assert not (_product(_ARMS, row) and _product(frozenset({_I.GITHUB_DISARM}), row))
    # P1new: a new head disarms an armed row; no unsent arm names an old head.
    if row.head_sha != pre.head_sha and (
        pre.armed is not None or pre.state is _S.ARMED
    ):
        assert _I.GITHUB_DISARM in kinds
        assert row.armed is None
    for arm in _product(_ARMS, row):
        assert arm.head_sha == row.head_sha

    # P2: no re-run of a product failure; one re-run per (head, check).
    for rerun in (i for i in emitted if i.kind is _I.GITHUB_RERUN):
        assert obs.verdict is not EnumHeadCheckVerdict.PRODUCT_FAILED
        for check in rerun.check_runs:
            key = (str(rerun.head_sha), check)
            assert key not in walk.reruns, key
            walk.reruns.add(key)

    # P3 and CompTracked: one command in flight, each id issued once and tracked.
    commands = [i for i in emitted if i.kind in _COMMANDS]
    assert len(commands) <= 1
    if obs.kind is _K.COMPANION_OUTCOME and obs.command_id == pre.companion.command_id:
        walk.answered.add(str(obs.command_id))
    for command in commands:
        answering = (
            obs.kind is _K.COMPANION_OUTCOME
            and obs.command_id == pre.companion.command_id
        )
        assert pre.companion.command_id is None or answering
        assert command.command_id not in walk.commands
        assert row.companion.command_id == command.command_id
        walk.commands.add(str(command.command_id))
    pending = row.companion.status is EnumPrLandingCompanionStatus.PENDING
    assert pending == (row.companion.command_id is not None)
    if pending:
        assert row.companion.command_id in walk.commands
        assert row.companion.command_id not in walk.answered

    # P4: one terminal per (PR, episode).
    entered_terminal = row.state in _TERMINAL and (
        pre.state not in _TERMINAL or out.trigger == CLOSED_EPISODE_TRIGGERS["41"]
    )
    if entered_terminal:
        walk.terminals[row.episode] = walk.terminals.get(row.episode, 0) + 1
        assert walk.terminals[row.episode] == 1, (row.episode, walk.terminals)

    # TimerFresh: an expiry applies to its own entry only, never in PARKED.
    if obs.kind is _K.BOUND_EXPIRED:
        assert pre.state is not _S.PARKED
        assert (obs.episode, obs.state_entry_generation) == (
            pre.episode,
            pre.state_entry_generation,
        )
        assert row.state is _S.NEEDS_AGENT
    moved = (row.state, row.head_sha, row.episode) != (
        pre.state,
        pre.head_sha,
        pre.episode,
    )
    assert row.state_entry_generation == pre.state_entry_generation + int(moved)

    # P7: a row in COMPANION_PENDING has its command in flight, and entering a
    # waiting state (or answering a verdict there) leaves something in flight.
    if row.state is _S.COMPANION_PENDING:
        assert row.companion.command_id is not None
    trigger = str(out.trigger)
    if row.state in _WAITING and (
        row.state is not pre.state or trigger.startswith("verdict_")
    ):
        sanctioned = trigger == "disarmed"  # row 35 lists no intent
        in_flight = bool(emitted) or row.state is _S.COMPANION_OPEN
        assert in_flight or row.companion.command_id is not None or sanctioned, trigger


def _run_walk(seed: int) -> dict[str, int]:
    handler = HandlerPrLandingReducer()
    walk = _Walk(rng=random.Random(seed))
    row: ModelPrLandingState | None = None
    seen: dict[str, int] = {}
    for step in range(STEPS):
        obs = None if row is None else _draw(walk, row)
        if row is None:
            obs = _obs(
                walk, _K.PUSHED, head_sha=head("h1"), source_seq=1, ticket_ids=TICKETS
            )
        if obs is None:
            assert row is not None
            row = _drain(row)
            continue
        request = ModelPrLandingReduceInput(state=row, observation=obs)
        out = handler.handle(request)
        assert handler.handle(request) == out, "the reducer is deterministic"
        pre = row if row is not None else out.state
        if row is None:
            assert out.trigger == "pushed", (seed, step, out.dropped_reason)
        else:
            try:
                _check(walk, pre, obs, out)
            except AssertionError as err:
                msg = f"seed {seed} step {step}: {obs.kind} in {pre.state} -> {out.trigger or out.dropped_reason}"
                raise AssertionError(msg) from err
        key = out.trigger or f"drop:{str(out.dropped_reason).split(':', 1)[0]}"
        seen[key] = seen.get(key, 0) + 1
        if row is not None and out.trigger is not None:
            edge = f"edge:{row.state.value}|{out.trigger}|{out.state.state.value}"
            seen[edge] = seen.get(edge, 0) + 1
        row = out.state
        if row.state is _S.MERGED and walk.rng.random() < 0.5:
            break
    return seen


def _run_walks(seeds: range) -> None:
    for seed in seeds:
        _run_walk(seed)


class TestSafetyPropertiesOverRandomWalks:
    """P1, P1arm, P1new, P2, P3, P4 per episode, CompTracked, TimerFresh and P7."""

    @pytest.mark.parametrize("block", range(6))
    def test_every_step_of_every_walk_keeps_the_properties(self, block: int) -> None:
        per_block = WALKS // 6
        for seed in range(block * per_block, (block + 1) * per_block):
            _run_walk(seed)

    def test_the_walks_reach_every_row_the_table_has(self) -> None:
        """Positive control: the properties above are not vacuous.

        Row 26 (a green verdict on a draft or held row) is the one row a walk
        from first sight cannot reach: every snapshot that sets draft or held
        parks the row or sends it through the evaluation, so a CHECKS_PENDING
        row is never draft or held here. Its counterexample case
        (F3-green-verdict-on-held-row) starts from that row instead.
        """
        seen: dict[str, int] = {}
        for seed in range(WALKS):
            for key, count in _run_walk(seed).items():
                seen[key] = seen.get(key, 0) + count
        wanted = {
            "pushed",
            "reopened",
            "evaluated_parked",
            "evaluated_companion_required",
            "evaluated_companion_in_flight",
            "evaluated_companion_open",
            "evaluated_checks_required",
            "companion_minted",
            "companion_declined",
            "companion_error_budget_left",
            "companion_error_budget_spent",
            "companion_outcome_recorded",
            "companion_merged",
            "companion_conflicting_budget_left",
            "companion_closed_unmerged",
            "converted_to_draft",
            "hold_applied",
            "ready_for_review",
            "hold_lifted",
            "title_edited",
            "verdict_stale_attempt",
            "verdict_green",
            "verdict_change_control_open_companion_merged",
            "verdict_change_control_open_companion_not_merged",
            "verdict_change_control_stale_budget_left",
            "verdict_rerunnable_budget_left",
            "verdict_update_branch",
            "verdict_real_red",
            "verdict_pending",
            "armed_confirmed",
            "disarmed",
            "merged",
            "closed",
            "completion_bound_expired",
            *CLOSED_EPISODE_TRIGGERS.values(),
            "drop:2",
            "drop:14",
            "drop:43",
            "drop:head_match",
            "drop:terminal",
        }
        missing = wanted - set(seen)
        assert not missing, sorted(missing)


# Edges a walk from first sight cannot take, with the handed-in row and the
# observation that takes each one. A pending companion only ever sits in
# COMPANION_PENDING, PARKED, NEEDS_AGENT or OBSERVED here (companion merged
# clears it), and a CHECKS_PENDING row is never draft or held (see row 26
# above), so these start from the row the table names.
_HANDED_IN_EDGES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    **{
        f"edge:{state}|companion_outcome_recorded|{state}": (
            {
                "state": state,
                "head": "h1",
                "seq": 1,
                "companion": {"status": "pending", "command_id": "c1"},
            },
            {"kind": "companion_outcome", "command_id": "c1", "outcome": "MINTED"},
        )
        for state in ("COMPANION_OPEN", "CHECKS_PENDING", "READY", "ARMED")
    },
    "edge:CHECKS_PENDING|verdict_green_draft_or_held|PARKED": (
        {"state": "CHECKS_PENDING", "head": "h1", "seq": 1, "held": True},
        {"kind": "head_checks", "head": "h1", "verdict": "green"},
    ),
}


def _contract_edges() -> set[str]:
    contract = yaml.safe_load(
        (
            FIXTURES.parents[2]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_pr_landing_orchestrator"
            / "contract.yaml"
        ).read_text(encoding="utf-8")
    )
    return {
        f"edge:{t['from_state']}|{t['trigger']}|{t['to_state']}"
        for t in contract["state_machine"]["transitions"]
    }


class TestEveryEdgeOfTheTable:
    """AC1: the handler takes every one of the contract's 92 edges."""

    def test_the_walks_and_the_handed_in_rows_take_every_contract_edge(self) -> None:
        taken: set[str] = set()
        for seed in range(WALKS):
            taken |= {k for k in _run_walk(seed) if k.startswith("edge:")}
        handler = HandlerPrLandingReducer()
        for edge, (start, spec) in _HANDED_IN_EDGES.items():
            row = start_row(start)
            out = handler.handle(
                ModelPrLandingReduceInput(state=row, observation=observation(spec, 0))
            )
            took = f"edge:{row.state.value}|{out.trigger}|{out.state.state.value}"
            assert took == edge, (edge, out.dropped_reason)
            taken.add(took)
        edges = _contract_edges()
        assert len(edges) == 92
        assert not edges - taken, sorted(edges - taken)
        assert (
            not {e for e in taken if e.startswith("edge:")}
            - edges
            - {
                f"edge:CLOSED|{t}|{to}"
                for t, to in (
                    (CLOSED_EPISODE_TRIGGERS["13"], "CLOSED"),
                    (CLOSED_EPISODE_TRIGGERS["16"], "CLOSED"),
                    (CLOSED_EPISODE_TRIGGERS["39"], "OBSERVED"),
                    (CLOSED_EPISODE_TRIGGERS["40"], "CLOSED"),
                    (CLOSED_EPISODE_TRIGGERS["41"], "MERGED"),
                )
            }
        ), "the handler took an edge the contract does not declare"


class TestAMutantThatArmsAHeldPrFails:
    """AC2: the properties are not vacuous for P1.

    The mutant ignores draft and held in the evaluation and in the verdict, so
    a held or draft PR reaches a green verdict and is armed. Either the P1
    assertions or the row model's own P1 refusal must stop the walks.
    """

    def test_the_walks_catch_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from omnimarket.nodes.node_pr_landing_reducer.handlers import (
            handler_pr_landing_reducer as module,
        )

        def _blind(fn: Any) -> Any:
            def mutant(row: ModelPrLandingState, obs: ModelPrLandingObservation) -> Any:
                return fn(row.model_copy(update={"draft": False, "held": False}), obs)

            return mutant

        monkeypatch.setattr(module, "_evaluate", _blind(module._evaluate))
        monkeypatch.setattr(module, "_verdict", _blind(module._verdict))
        with pytest.raises((AssertionError, ValidationError)):
            _run_walks(range(WALKS))

    def test_the_p1_assertions_catch_an_arm_the_row_model_cannot_see(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An arm intent for a held PR that never sets ``armed`` passes the row
        model's refusal, so only the walk's P1 assertions can stop it."""
        from omnimarket.nodes.node_pr_landing_reducer.handlers import (
            handler_pr_landing_reducer as module,
        )

        evaluate, verdict = module._evaluate, module._verdict

        def blind_evaluate(
            row: ModelPrLandingState, obs: ModelPrLandingObservation
        ) -> Any:
            return evaluate(row.model_copy(update={"draft": False, "held": False}), obs)

        def arming_verdict(
            row: ModelPrLandingState, obs: ModelPrLandingObservation
        ) -> Any:
            step = verdict(row, obs)
            if getattr(step, "trigger", None) == "verdict_green_draft_or_held":
                arm = module._intent(row, _I.GITHUB_ARM)
                return module._Move("verdict_green", _S.READY, intents=(arm,))
            return step

        monkeypatch.setattr(module, "_evaluate", blind_evaluate)
        monkeypatch.setattr(module, "_verdict", arming_verdict)
        with pytest.raises(AssertionError):
            _run_walks(range(WALKS))
