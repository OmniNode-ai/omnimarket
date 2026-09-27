# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrLandingReducer: the pure transition function of the PR landing FSM.

Definition-B: ``handle(ModelPrLandingReduceInput) -> ModelPrLandingReduceOutput``.
No clock (time comes from the observation), no I/O, no language model. It
applies the 43 rows of revision 1 of plan section 5.1 (section 3 of the
model-checked revision), as frozen in tests/fixtures/pr_landing/
fsm_transitions.yaml and the orchestrator contract's ``state_machine``.

The order the rows are applied in:

1. A MERGED row drops everything. A CLOSED row takes only the
   ``closed_episode_rules`` (rows 13, 16, 39, 40, 41); the rest drop.
2. The completion-bound expiry applies only when its (episode,
   state_entry_generation) equals the row's and the row is not PARKED
   (R2a, R2b); otherwise it is dropped (row 43).
3. A companion outcome correlates by ``command_id`` (F5): any other command's
   outcome is dropped (row 14). The one in flight moves COMPANION_PENDING
   (rows 9 to 12) and is recorded in place in every other state that has a
   row for it (row 13, R1).
4. Companion facts with no product head (merged, conflicting, closed) and
   merged and closed are exempt from the head-match rule (rows 15 to 19, 37,
   38). closed, like every snapshot, applies only when newer by the ordering
   key (F2).
5. The evaluation runs only in OBSERVED (rows 4 to 8). OBSERVED is evaluated
   at once, so any other head-bound observation there is dropped.
6. A push applies only when it names a head other than ``head_sha`` and is
   newer by the ordering key (row 1, F1); it disarms if armed (R4).
7. Every other observation must name the row's head (the head-match drop
   rule), and a snapshot must be newer by the ordering key (row 2, F2).

Row bookkeeping: ``seq`` advances by one on every applied row;
``state_entry_generation`` advances, and ``entered_state_at`` is taken from
the observation, whenever the state, the head or the episode changes (R2b).
The row's in-row outbox takes every intent, and a disarm removes an unsent
arm of the product PR while an arm removes an unsent disarm (F6). ``armed`` is
set when the arm intent is written and cleared when the disarm is (R4).

A snapshot that leaves the row draft or held, or on a new head, while it is
armed always writes a disarm: the row model refuses an armed draft or held
row (P1), and F3 and R4 both say the arm goes. The only row where that adds
an intent the table does not list is a reopen (row 3) whose snapshot reads a
draft or hold on an armed row.

Decisions for inputs the table does not cover (``open_rows`` in the fixture)
drop the observation and leave the escalation to the completion bound, never
invent an edge: COMPANION_OPEN conflicting with the regenerate budget spent;
COMPANION_OPEN closed unmerged, and an evaluation that must derive, with the
derive budget spent.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from omnimarket.events.pr_head_check.enum_head_check_verdict import (
    EnumHeadCheckVerdict,
)
from omnimarket.events.pr_landing.enum_pr_landing_agent_reason import (
    EnumPrLandingAgentReason,
)
from omnimarket.events.pr_landing.enum_pr_landing_arm_method import (
    EnumPrLandingArmMethod,
)
from omnimarket.events.pr_landing.enum_pr_landing_companion_outcome import (
    EnumPrLandingCompanionOutcome,
)
from omnimarket.events.pr_landing.enum_pr_landing_companion_status import (
    EnumPrLandingCompanionStatus,
)
from omnimarket.events.pr_landing.enum_pr_landing_intent_kind import (
    EnumPrLandingIntentKind,
)
from omnimarket.events.pr_landing.enum_pr_landing_observation_kind import (
    EnumPrLandingObservationKind,
)
from omnimarket.events.pr_landing.enum_pr_landing_state import (
    EnumPrLandingState,
)
from omnimarket.events.pr_landing.model_pr_landing_check_attempt import (
    ModelPrLandingCheckAttempt,
)
from omnimarket.events.pr_landing.model_pr_landing_intent import (
    ModelPrLandingIntent,
)
from omnimarket.events.pr_landing.model_pr_landing_observation import (
    SNAPSHOT_KINDS,
    ModelPrLandingObservation,
)
from omnimarket.events.pr_landing.model_pr_landing_state import (
    ModelPrLandingBudgets,
    ModelPrLandingCompanion,
    ModelPrLandingState,
)
from omnimarket.nodes.node_pr_landing_reducer.models.model_pr_landing_reduce_input import (
    ModelPrLandingReduceInput,
)
from omnimarket.nodes.node_pr_landing_reducer.models.model_pr_landing_reduce_output import (
    ModelPrLandingReduceOutput,
)

_S = EnumPrLandingState
_K = EnumPrLandingObservationKind
_I = EnumPrLandingIntentKind
_V = EnumHeadCheckVerdict
_C = EnumPrLandingCompanionStatus
_O = EnumPrLandingCompanionOutcome
_R = EnumPrLandingAgentReason

# The triggers taken by the rows out of CLOSED. They are not core FSM edges
# (omnibase_core refuses an edge out of a terminal state); the orchestrator
# applies them outside the FSM, and the transitioned event carries these names.
CLOSED_EPISODE_TRIGGERS: dict[str, str] = {
    "13": "closed_companion_outcome_recorded",
    "16": "closed_companion_merged",
    "39": "closed_reopened",
    "40": "closed_closed",
    "41": "closed_merged",
}

# The drop codes a dropped_reason starts with: a drop_rules row, or one of these.
DROP_HEAD_MATCH = "head_match"
DROP_TERMINAL = "terminal"
DROP_NO_ROW = "no_row"
DROP_PROMPT = "prompt"
DROP_BUDGET = "budget"

_PARKS_ON_DRAFT_OR_HOLD = frozenset(
    {_S.COMPANION_PENDING, _S.COMPANION_OPEN, _S.CHECKS_PENDING, _S.READY, _S.ARMED}
)
_RECORDS_OUTCOME = frozenset(
    {
        _S.PARKED,
        _S.COMPANION_OPEN,
        _S.CHECKS_PENDING,
        _S.READY,
        _S.ARMED,
        _S.NEEDS_AGENT,
    }
)
_RECORDS_COMPANION_MERGED = frozenset(
    {_S.PARKED, _S.CHECKS_PENDING, _S.READY, _S.ARMED, _S.NEEDS_AGENT}
)
_DRAFT_OR_HOLD = frozenset({_K.CONVERTED_TO_DRAFT, _K.HOLD_APPLIED})
_READY_OR_UNHOLD = frozenset({_K.READY_FOR_REVIEW, _K.HOLD_LIFTED, _K.TITLE_EDITED})
_PRODUCT_ARMS = frozenset({_I.GITHUB_ARM, _I.GITHUB_ENQUEUE})
_RERUNNABLE = frozenset({_V.TIMED_OUT, _V.RUNNER_INFRA, _V.CANCELLED})
_UPDATE_BRANCH = frozenset({_V.STALE_CALLER_PIN, _V.BEHIND_REQUIRED})
_POLL = "after_poll_interval"


@dataclass(frozen=True)
class _Move:
    """One applied row: the trigger, the target, the row changes, the intents."""

    trigger: str
    to: EnumPrLandingState
    changes: dict[str, Any] = field(default_factory=dict)
    intents: tuple[ModelPrLandingIntent, ...] = ()


@dataclass(frozen=True)
class _Drop:
    code: str
    why: str


_Step = _Move | _Drop


class HandlerPrLandingReducer:
    """Maps (row, observation) to the next row, the edge taken and the intents."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["REDUCER"]:
        return "REDUCER"

    def handle(self, request: ModelPrLandingReduceInput) -> ModelPrLandingReduceOutput:
        obs = request.observation
        row = request.state or _first_sight(obs)
        step = _decide(row, obs)
        if isinstance(step, _Drop):
            return ModelPrLandingReduceOutput(
                state=row, dropped_reason=f"{step.code}: {step.why}"
            )
        return ModelPrLandingReduceOutput(
            state=_apply(row, obs, step), trigger=step.trigger, intents=step.intents
        )


# --- the rows ---------------------------------------------------------------


def _decide(row: ModelPrLandingState, obs: ModelPrLandingObservation) -> _Step:
    if row.state is _S.MERGED:
        return _Drop(DROP_TERMINAL, "MERGED is final")
    if row.state is _S.CLOSED:
        return _closed_episode(row, obs)
    kind = obs.kind
    if kind is _K.BOUND_EXPIRED:
        return _bound_expired(row, obs)
    if kind is _K.COMPANION_OUTCOME:
        return _companion_outcome(row, obs)
    if kind is _K.COMPANION_MERGED:
        return _companion_merged(row, obs)
    if kind in (_K.COMPANION_CONFLICTING, _K.COMPANION_CLOSED):
        return _companion_open_fact(row, obs)
    if kind is _K.MERGED:
        return _Move("merged", _S.MERGED, _seq_changes(row, obs))
    if kind is _K.CLOSED:
        if not _newer(row, obs):
            return _Drop("2", "closed not newer by the ordering key")
        return _Move("closed", _S.CLOSED, {"source_seq": obs.source_seq})
    if kind is _K.EVALUATION:
        if row.state is not _S.OBSERVED:
            return _Drop(
                DROP_NO_ROW, f"evaluation applies only in OBSERVED, not {row.state}"
            )
        return _evaluate(row, obs)
    if kind is _K.PUSHED:
        return _pushed(row, obs)
    if row.state is _S.OBSERVED and kind is not _K.REOPENED:
        return _Drop(
            DROP_NO_ROW, "OBSERVED is evaluated at once; nothing else applies there"
        )
    if obs.head_sha is None or obs.head_sha != row.head_sha:
        return _Drop(DROP_HEAD_MATCH, "the observation is not about the row's head")
    if kind in SNAPSHOT_KINDS and not _newer(row, obs):
        return _Drop("2", "a snapshot at the row's head not newer by the ordering key")
    if kind is _K.REOPENED:
        return _snapshot_move("reopened", _S.OBSERVED, row, obs)
    if kind in _DRAFT_OR_HOLD:
        return _draft_or_hold(row, obs)
    if kind in _READY_OR_UNHOLD:
        return _ready_or_unhold(row, obs)
    if kind is _K.HEAD_CHECKS:
        return _verdict(row, obs)
    if kind is _K.ARMED_CONFIRMED and row.state is _S.READY:
        return _Move("armed_confirmed", _S.ARMED)
    if kind is _K.DISARMED and row.state is _S.ARMED:
        return _Move("disarmed", _S.CHECKS_PENDING, {"armed": None})
    return _Drop(DROP_NO_ROW, f"no row for {kind} in {row.state}")


def _closed_episode(row: ModelPrLandingState, obs: ModelPrLandingObservation) -> _Step:
    """The rows out of CLOSED (G5, F10, R1, R4): a terminal for its episode only."""
    kind = obs.kind
    if kind is _K.COMPANION_OUTCOME:
        if not _in_flight(row, obs):
            return _Drop("14", "an outcome for a command not in flight")
        trigger = CLOSED_EPISODE_TRIGGERS["13"]
        return _Move(trigger, _S.CLOSED, _record_outcome(row, obs))
    if kind is _K.COMPANION_MERGED:
        trigger = CLOSED_EPISODE_TRIGGERS["16"]
        return _Move(trigger, _S.CLOSED, _record_companion_merged(obs))
    if kind is _K.MERGED:
        changes = {**_seq_changes(row, obs), "episode": row.episode + 1}
        return _Move(CLOSED_EPISODE_TRIGGERS["41"], _S.MERGED, changes)
    if kind is _K.BOUND_EXPIRED:
        return _Drop("43", "no completion bound in CLOSED")
    if kind not in SNAPSHOT_KINDS:
        return _Drop(DROP_TERMINAL, f"CLOSED takes no {kind}")
    if obs.head_sha is None and kind is not _K.CLOSED:
        return _Drop(DROP_PROMPT, "a snapshot names its head; read the PR first")
    if not _newer(row, obs):
        return _Drop("2", "a snapshot not newer by the ordering key")
    if kind is _K.CLOSED:
        trigger = CLOSED_EPISODE_TRIGGERS["40"]
        return _Move(trigger, _S.CLOSED, {"source_seq": obs.source_seq})
    move = _snapshot_move(CLOSED_EPISODE_TRIGGERS["39"], _S.OBSERVED, row, obs)
    return _Move(
        move.trigger,
        move.to,
        {**move.changes, "episode": row.episode + 1},
        move.intents,
    )


def _bound_expired(row: ModelPrLandingState, obs: ModelPrLandingObservation) -> _Step:
    """Rows 42 and 43: the bound is tied to the state entry it was set for."""
    if row.state is _S.PARKED:
        return _Drop("43", "PARKED has no completion bound")
    entry = (obs.episode, obs.state_entry_generation)
    if entry != (row.episode, row.state_entry_generation):
        return _Drop("43", f"the expiry is for entry {entry}, not this one")
    stalled = _agent(row, _R.STALLED, row.state.value)
    return _Move("completion_bound_expired", _S.NEEDS_AGENT, intents=(stalled,))


def _companion_outcome(
    row: ModelPrLandingState, obs: ModelPrLandingObservation
) -> _Step:
    """Rows 9 to 14: correlated by command_id; recorded in every state with a row."""
    if not _in_flight(row, obs):
        return _Drop("14", "an outcome for a command not in flight")
    if row.state in _RECORDS_OUTCOME:
        return _Move("companion_outcome_recorded", row.state, _record_outcome(row, obs))
    if row.state is not _S.COMPANION_PENDING:
        return _Drop(DROP_NO_ROW, f"no outcome row in {row.state}")
    outcome = obs.companion_outcome
    if outcome is _O.MINTED:
        intents: list[ModelPrLandingIntent] = []
        if not obs.companion_armed:
            intents.append(_intent(row, _I.GITHUB_ARM, target_pr=obs.occ_pr))
        if not obs.companion_stamped:
            intents.append(_intent(row, _I.COMPANION_VERIFY))
        return _Move(
            "companion_minted",
            _S.COMPANION_OPEN,
            _record_outcome(row, obs),
            tuple(intents),
        )
    if outcome is _O.DECLINED:
        declined = _agent(row, _R.COMPANION_DECLINED, obs.detail)
        return _Move(
            "companion_declined",
            _S.NEEDS_AGENT,
            _record_outcome(row, obs),
            (declined,),
        )
    if row.budgets.derive_left > 0:
        changes, derive = _new_command(row, _I.COMPANION_DERIVE, occ_pr=None)
        return _Move(
            "companion_error_budget_left", _S.COMPANION_PENDING, changes, (derive,)
        )
    error = _agent(row, _R.COMPANION_ERROR, None)
    return _Move(
        "companion_error_budget_spent",
        _S.NEEDS_AGENT,
        _record_outcome(row, obs),
        (error,),
    )


def _companion_merged(
    row: ModelPrLandingState, obs: ModelPrLandingObservation
) -> _Step:
    """Rows 15, 16 and 19: a ticket fact with no head, recorded in every state."""
    changes = _record_companion_merged(obs)
    if row.state in (_S.COMPANION_PENDING, _S.COMPANION_OPEN):
        read = _intent(row, _I.GITHUB_READ_HEAD_CHECKS)
        return _Move("companion_merged", _S.CHECKS_PENDING, changes, (read,))
    if row.state in _RECORDS_COMPANION_MERGED:
        return _Move("companion_merged", row.state, changes)
    return _Drop(DROP_NO_ROW, f"no companion-merged row in {row.state}")


def _companion_open_fact(
    row: ModelPrLandingState, obs: ModelPrLandingObservation
) -> _Step:
    """Rows 17 and 18: a companion read conflicting or closed unmerged."""
    if row.state is not _S.COMPANION_OPEN:
        return _Drop(DROP_NO_ROW, f"no row for {obs.kind} in {row.state}")
    if obs.kind is _K.COMPANION_CONFLICTING:
        if row.budgets.regenerate_left == 0:
            return _Drop(DROP_BUDGET, "regenerate budget spent; the bound escalates")
        changes, command = _new_command(row, _I.COMPANION_REGENERATE, occ_pr=obs.occ_pr)
        return _Move(
            "companion_conflicting_budget_left",
            _S.COMPANION_PENDING,
            changes,
            (command,),
        )
    if row.budgets.derive_left == 0:
        return _Drop(DROP_BUDGET, "derive budget spent; the bound escalates")
    changes, command = _new_command(row, _I.COMPANION_DERIVE, occ_pr=None)
    return _Move("companion_closed_unmerged", _S.COMPANION_PENDING, changes, (command,))


def _evaluate(row: ModelPrLandingState, obs: ModelPrLandingObservation) -> _Step:
    """Rows 4 to 8: OBSERVED's evaluation, which reads the recorded companion."""
    if row.draft or row.held or not row.ticket_ids or not obs.base_served:
        changes, intents = _disarm(row)
        return _Move("evaluated_parked", _S.PARKED, changes, intents)
    status = row.companion.status
    if status is _C.PENDING:
        return _Move("evaluated_companion_in_flight", _S.COMPANION_PENDING)
    if status in (_C.OPEN, _C.CONFLICTING):
        verify = _intent(row, _I.COMPANION_VERIFY)
        return _Move("evaluated_companion_open", _S.COMPANION_OPEN, intents=(verify,))
    if status is not _C.MERGED and obs.companion_required:
        if row.budgets.derive_left == 0:
            return _Drop(
                DROP_BUDGET, "derive budget spent; the OBSERVED bound escalates"
            )
        changes, derive = _new_command(row, _I.COMPANION_DERIVE, occ_pr=None)
        return _Move(
            "evaluated_companion_required", _S.COMPANION_PENDING, changes, (derive,)
        )
    read = _intent(row, _I.GITHUB_READ_HEAD_CHECKS)
    return _Move("evaluated_checks_required", _S.CHECKS_PENDING, intents=(read,))


def _pushed(row: ModelPrLandingState, obs: ModelPrLandingObservation) -> _Step:
    """Row 1: a new head, newer by the ordering key (F1), disarming if armed (R4)."""
    if obs.head_sha is None or obs.source_seq is None:
        return _Drop(DROP_PROMPT, "a push prompt carries no head or key; read the PR")
    if obs.head_sha == row.head_sha:
        if not _newer(row, obs):
            return _Drop(
                "2", "a snapshot at the row's head not newer by the ordering key"
            )
        return _Drop(DROP_NO_ROW, "a push at the row's head is not a new head")
    if not _newer(row, obs):
        return _Drop(DROP_HEAD_MATCH, "another head, not newer by the ordering key")
    return _snapshot_move("pushed", _S.OBSERVED, row, obs)


def _draft_or_hold(row: ModelPrLandingState, obs: ModelPrLandingObservation) -> _Step:
    """Rows 20, 22 and 36 (F3): park the waiting states; disarm in NEEDS_AGENT."""
    if row.state in _PARKS_ON_DRAFT_OR_HOLD:
        return _snapshot_move(obs.kind.value, _S.PARKED, row, obs)
    if row.state is _S.NEEDS_AGENT:
        return _snapshot_move(obs.kind.value, _S.NEEDS_AGENT, row, obs)
    return _Drop(DROP_NO_ROW, f"no {obs.kind} row in {row.state}")


def _ready_or_unhold(row: ModelPrLandingState, obs: ModelPrLandingObservation) -> _Step:
    """Rows 21 and 23 (R1, R2c): re-evaluate once neither draft nor held."""
    draft, held = _read_flags(row, obs)
    if row.state is _S.PARKED:
        if draft or held:
            return _Drop(DROP_NO_ROW, "the snapshot leaves the row draft or held")
        return _snapshot_move(obs.kind.value, _S.OBSERVED, row, obs)
    recorded = row.draft or row.held
    if (
        row.state is _S.NEEDS_AGENT
        and obs.kind is not _K.TITLE_EDITED
        and recorded
        and not (draft or held)
    ):
        return _snapshot_move(obs.kind.value, _S.OBSERVED, row, obs)
    return _Drop(DROP_NO_ROW, f"no {obs.kind} row in {row.state} for this snapshot")


def _verdict(row: ModelPrLandingState, obs: ModelPrLandingObservation) -> _Step:
    """Rows 24 to 33: one head-check verdict in CHECKS_PENDING."""
    if row.state is not _S.CHECKS_PENDING:
        return _Drop(
            DROP_NO_ROW, f"a verdict applies only in CHECKS_PENDING, not {row.state}"
        )
    expected = {a.check: a.attempt for a in row.expected_attempts}
    if any(a.attempt < expected.get(a.check, 0) for a in obs.check_attempts):
        read = _intent(row, _I.GITHUB_READ_HEAD_CHECKS)
        return _Move("verdict_stale_attempt", _S.CHECKS_PENDING, intents=(read,))
    verdict = obs.verdict
    if verdict is _V.GREEN:
        if row.draft or row.held:
            return _Move("verdict_green_draft_or_held", _S.PARKED)
        if obs.arm_method is None:
            return _Move("verdict_green", _S.READY)
        queue = obs.arm_method is EnumPrLandingArmMethod.QUEUE
        kind = _I.GITHUB_ENQUEUE if queue else _I.GITHUB_ARM
        arm = _intent(row, kind)
        return _Move("verdict_green", _S.READY, {"armed": obs.arm_method}, (arm,))
    if verdict is _V.CHANGE_CONTROL_OPEN:
        if row.companion.status is _C.MERGED:
            read = _intent(row, _I.GITHUB_READ_HEAD_CHECKS, detail=_POLL)
            return _Move(
                "verdict_change_control_open_companion_merged",
                _S.CHECKS_PENDING,
                intents=(read,),
            )
        return _Move("verdict_change_control_open_companion_not_merged", _S.OBSERVED)
    if verdict is _V.CHANGE_CONTROL_STALE or verdict in _RERUNNABLE:
        spent = set(row.budgets.rerun_checks) & set(obs.rerun_checks)
        if spent:
            return _real_red(row, f"re-run budget spent: {', '.join(sorted(spent))}")
        trigger = (
            "verdict_change_control_stale_budget_left"
            if verdict is _V.CHANGE_CONTROL_STALE
            else "verdict_rerunnable_budget_left"
        )
        return _Move(trigger, _S.CHECKS_PENDING, *_rerun(row, obs))
    if verdict in _UPDATE_BRANCH:
        left = row.budgets.update_branch_left
        if left == 0:
            return _real_red(row, "update-branch budget spent")
        budgets = row.budgets.model_copy(update={"update_branch_left": left - 1})
        update = _intent(row, _I.GITHUB_UPDATE_BRANCH)
        return _Move(
            "verdict_update_branch", _S.CHECKS_PENDING, {"budgets": budgets}, (update,)
        )
    if verdict is _V.PRODUCT_FAILED:
        return _real_red(row, "product_failed")
    read = _intent(row, _I.GITHUB_READ_HEAD_CHECKS, detail=_POLL)
    return _Move("verdict_pending", _S.CHECKS_PENDING, intents=(read,))


# --- helpers ----------------------------------------------------------------


def _first_sight(obs: ModelPrLandingObservation) -> ModelPrLandingState:
    """The initial OBSERVED row with no head: any snapshot's head is new to it."""
    return ModelPrLandingState(
        repository=obs.repository,
        pr_number=obs.pr_number,
        state=_S.OBSERVED,
        seq=0,
        entered_state_at=obs.observed_at,
        landing_key=obs.landing_key,
    )


def _newer(row: ModelPrLandingState, obs: ModelPrLandingObservation) -> bool:
    return obs.source_seq is not None and obs.source_seq > row.source_seq


def _in_flight(row: ModelPrLandingState, obs: ModelPrLandingObservation) -> bool:
    return (
        row.companion.command_id is not None
        and obs.command_id == row.companion.command_id
    )


def _seq_changes(
    row: ModelPrLandingState, obs: ModelPrLandingObservation
) -> dict[str, Any]:
    if _newer(row, obs):
        return {"source_seq": obs.source_seq}
    return {}


def _read_flags(
    row: ModelPrLandingState, obs: ModelPrLandingObservation
) -> tuple[bool, bool]:
    """The draft and held values after the snapshot: read, stated by the kind, or kept."""
    implied_draft = {_K.CONVERTED_TO_DRAFT: True, _K.READY_FOR_REVIEW: False}
    implied_held = {_K.HOLD_APPLIED: True, _K.HOLD_LIFTED: False}
    draft = (
        obs.draft if obs.draft is not None else implied_draft.get(obs.kind, row.draft)
    )
    held = obs.held if obs.held is not None else implied_held.get(obs.kind, row.held)
    return draft, held


def _snapshot_move(
    trigger: str,
    to: EnumPrLandingState,
    row: ModelPrLandingState,
    obs: ModelPrLandingObservation,
) -> _Move:
    """Apply a snapshot: head, key, flags and tickets; a new head resets its budgets.

    Disarms when armed and the snapshot moves the head (R4) or leaves the row
    draft or held (F3).
    """
    draft, held = _read_flags(row, obs)
    changes: dict[str, Any] = {
        "source_seq": obs.source_seq,
        "draft": draft,
        "held": held,
    }
    if obs.ticket_ids:
        changes["ticket_ids"] = obs.ticket_ids
    new_head = obs.head_sha is not None and obs.head_sha != row.head_sha
    if new_head:
        changes["head_sha"] = obs.head_sha
        changes["budgets"] = row.budgets.model_copy(
            update={
                "update_branch_left": ModelPrLandingBudgets().update_branch_left,
                "rerun_checks": (),
            }
        )
        changes["expected_attempts"] = ()
        changes["merge_state"] = None
    intents: tuple[ModelPrLandingIntent, ...] = ()
    if new_head or draft or held:
        disarm_changes, intents = _disarm(row)
        changes.update(disarm_changes)
    return _Move(trigger, to, changes, intents)


def _disarm(
    row: ModelPrLandingState,
) -> tuple[dict[str, Any], tuple[ModelPrLandingIntent, ...]]:
    """Disarm if armed, where armed means (R4) an arm queued in the outbox, sent
    and not yet answered, or confirmed: ``armed`` set, an unsent arm of the
    product PR in the outbox, or the row in ARMED (row 36 always disarms).
    """
    queued = any(q.target_pr is None and q.kind in _PRODUCT_ARMS for q in row.outbox)
    if row.armed is None and row.state is not _S.ARMED and not queued:
        return {}, ()
    return {"armed": None}, (_intent(row, _I.GITHUB_DISARM),)


def _record_outcome(
    row: ModelPrLandingState, obs: ModelPrLandingObservation
) -> dict[str, Any]:
    """R1: MINTED records open, DECLINED declined, ERROR none; command_id clears."""
    outcome = obs.companion_outcome
    if outcome is _O.MINTED:
        companion = ModelPrLandingCompanion(occ_pr=obs.occ_pr, status=_C.OPEN)
        return {"companion": companion, "stamp_present": bool(obs.companion_stamped)}
    status = _C.DECLINED if outcome is _O.DECLINED else _C.NONE
    return {
        "companion": ModelPrLandingCompanion(occ_pr=row.companion.occ_pr, status=status)
    }


def _record_companion_merged(obs: ModelPrLandingObservation) -> dict[str, Any]:
    return {"companion": ModelPrLandingCompanion(occ_pr=obs.occ_pr, status=_C.MERGED)}


def _new_command(
    row: ModelPrLandingState, kind: EnumPrLandingIntentKind, *, occ_pr: int | None
) -> tuple[dict[str, Any], ModelPrLandingIntent]:
    """A derive or regenerate recorded by command_id (F4), charged to its budget.

    The id is a pure function of the row: its key, episode and the seq this
    transition takes, so it is unique per row transition and never read from
    a clock or a random source.
    """
    command_id = f"{row.landing_key}:{row.episode}:{row.seq + 1}:{kind.value}"
    budgets = row.budgets
    if kind is _I.COMPANION_DERIVE:
        budgets = budgets.model_copy(update={"derive_left": budgets.derive_left - 1})
    else:
        budgets = budgets.model_copy(
            update={"regenerate_left": budgets.regenerate_left - 1}
        )
    companion = ModelPrLandingCompanion(
        occ_pr=occ_pr, status=_C.PENDING, command_id=command_id
    )
    changes = {"companion": companion, "budgets": budgets}
    return changes, _intent(row, kind, command_id=command_id)


def _rerun(
    row: ModelPrLandingState, obs: ModelPrLandingObservation
) -> tuple[dict[str, Any], tuple[ModelPrLandingIntent, ...]]:
    """F7: re-run the named runs and record the attempt each re-run starts."""
    read_attempts = {a.check: a.attempt for a in obs.check_attempts}
    expected = {a.check: a.attempt for a in row.expected_attempts}
    for check in obs.rerun_checks:
        expected[check] = read_attempts.get(check, 1) + 1
    budgets = row.budgets.model_copy(
        update={"rerun_checks": (*row.budgets.rerun_checks, *obs.rerun_checks)}
    )
    changes = {
        "budgets": budgets,
        "expected_attempts": tuple(
            ModelPrLandingCheckAttempt(check=c, attempt=a)
            for c, a in sorted(expected.items())
        ),
    }
    rerun = _intent(row, _I.GITHUB_RERUN, check_runs=obs.rerun_checks)
    return changes, (rerun,)


def _real_red(row: ModelPrLandingState, detail: str) -> _Move:
    red = _agent(row, _R.REAL_RED, f"checks: {detail}")
    return _Move("verdict_real_red", _S.NEEDS_AGENT, intents=(red,))


def _agent(
    row: ModelPrLandingState, reason: EnumPrLandingAgentReason, detail: str | None
) -> ModelPrLandingIntent:
    return _intent(row, _I.AGENT_NEEDED, agent_reason=reason, detail=detail)


def _intent(
    row: ModelPrLandingState, kind: EnumPrLandingIntentKind, **fields: Any
) -> ModelPrLandingIntent:
    return ModelPrLandingIntent(
        kind=kind,
        repository=row.repository,
        pr_number=row.pr_number,
        head_sha=row.head_sha,
        **fields,
    )


def _enqueue(
    outbox: tuple[ModelPrLandingIntent, ...], intents: Iterable[ModelPrLandingIntent]
) -> tuple[ModelPrLandingIntent, ...]:
    """F6: a disarm removes an unsent arm of the product PR, and an arm a disarm."""
    queued = list(outbox)
    for new in intents:
        if new.target_pr is None and new.kind is _I.GITHUB_DISARM:
            queued = [
                q
                for q in queued
                if not (q.target_pr is None and q.kind in _PRODUCT_ARMS)
            ]
        elif new.target_pr is None and new.kind in _PRODUCT_ARMS:
            queued = [
                q
                for q in queued
                if not (q.target_pr is None and q.kind is _I.GITHUB_DISARM)
            ]
        queued.append(new)
    return tuple(queued)


def _apply(
    row: ModelPrLandingState, obs: ModelPrLandingObservation, move: _Move
) -> ModelPrLandingState:
    """The next row: changes, target, seq, the state-entry generation and the outbox."""
    data: dict[str, Any] = {**dict(row), **move.changes, "state": move.to}
    entered = (
        move.to is not row.state
        or data["head_sha"] != row.head_sha
        or data["episode"] != row.episode
    )
    data["seq"] = row.seq + 1
    if entered:
        data["state_entry_generation"] = row.state_entry_generation + 1
        data["entered_state_at"] = obs.observed_at
    data["outbox"] = _enqueue(row.outbox, move.intents)
    return ModelPrLandingState.model_validate(data)


__all__: list[str] = [
    "CLOSED_EPISODE_TRIGGERS",
    "DROP_BUDGET",
    "DROP_HEAD_MATCH",
    "DROP_NO_ROW",
    "DROP_PROMPT",
    "DROP_TERMINAL",
    "HandlerPrLandingReducer",
]
