# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane work reducer: delta(state, event) -> (new_state, intents) (OMN-26706).

The contract's ``state_machine`` is the program (plan section 3.4, modelled in
formal/lane_work_loop). The handler parses it once into ``ModelFSMSubcontract``,
looks an edge up by state, trigger, conditions and priority, and refuses an input
that has none. It keeps no table of states or edges of its own: which states are
worker-owned, which deadlines a state arms and where help starts are all read
from the contract's state types and entry actions.

It applies the repeat rule, the help budget and the armed deadlines, and builds
intents with plain action ids. It performs no I/O (the contract is loaded by
``omnimarket.handlers.rules_lane_work_reducer``) and reads no clock: ``now`` is an input. The repeat bound, deadlines,
wait budget, help budget and help chain arrive in ``ModelLaneWorkReducerConfig``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any, Literal

from omnibase_core.models.contracts.subcontracts.model_fsm_state_definition import (
    ModelFSMStateDefinition,
)
from omnibase_core.models.contracts.subcontracts.model_fsm_state_transition import (
    ModelFSMStateTransition,
)
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.models.lane_work import ModelLaneWorkChange, build_action_id

from omnimarket.handlers.rules_lane_work_reducer import load_lane_work_fsm
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_fold_input import (
    ModelLaneWorkFoldInput,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_intent import (
    ModelLaneWorkIntent,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reduce_request import (
    ModelLaneWorkReduceRequest,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reduce_result import (
    ModelLaneWorkReduceResult,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_config import (
    ModelLaneWorkReducerConfig,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_state import (
    ModelLaneWorkReducerState,
)

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]

# Event kinds (not states). A refused answer in a help state counts as help_failed.
_HELP_ANSWER_KINDS = frozenset({"help_cause_found", "help_retry", "help_stop"})
_ALWAYS_ADMISSIBLE = frozenset(
    {
        "push_refused",
        "proof_failed",
        "proof_post_failed",
        "handoff_failed",
        "unclassified",
    }
)
_REPEAT = "repeat_bound_reached"
_DEADLINE = "deadline_passed"
_STATUS = "status_requested"
_HEAD_TAKERS = frozenset({"head_moved", "pushed"})
_OBSERVATIONS = frozenset({"check_red", "check_pending", "check_green"})


def _moment(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _heads_match(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left.startswith(right) or right.startswith(left)


class _Machine:
    """The contract's machine, parsed once: states by name, edges by (from, trigger)."""

    def __init__(self, subcontract: ModelFSMSubcontract) -> None:
        self.subcontract = subcontract
        self.states: dict[str, ModelFSMStateDefinition] = {
            definition.state_name: definition for definition in subcontract.states
        }
        grouped: dict[tuple[str, str], list[ModelFSMStateTransition]] = {}
        for transition in subcontract.transitions:
            grouped.setdefault((transition.from_state, transition.trigger), []).append(
                transition
            )
        self.edges: dict[tuple[str, str], tuple[ModelFSMStateTransition, ...]] = {
            key: tuple(sorted(edges, key=lambda edge: -edge.priority))
            for key, edges in grouped.items()
        }


@lru_cache(maxsize=1)
def _load_machine() -> _Machine:
    return _Machine(load_lane_work_fsm())


def deadline_event(
    deadline: str, at: datetime | str | None, *, state: ModelLaneWorkReducerState
) -> ModelLaneWorkFoldInput:
    """The tick's ``deadline_passed`` for a deadline, with the arming the state holds."""
    attempt = state.armed_deadlines.get(deadline, 0)
    return ModelLaneWorkFoldInput(
        kind=_DEADLINE,
        deadline=deadline,
        deadline_attempt=attempt,
        input_id=f"{state.workflow_id}-deadline-{deadline}-{attempt}",
        observed_at=_moment(at),
    )


def _holds(expression: str, context: dict[str, str]) -> bool:
    """Evaluate a three-token contract condition: ``field operator value``."""
    field, operator, value = expression.split()
    actual = context.get(field, "")
    if operator == "==":
        return actual == value
    if operator == "!=":
        return actual != value
    if operator == "in":
        return actual in value.split("|")
    raise ValueError(f"unsupported condition operator {operator!r} in {expression!r}")


class HandlerLaneWorkReducer:
    """Pure reducer: ``handle(ModelLaneWorkReduceRequest) -> ModelLaneWorkReduceResult``."""

    def __init__(self) -> None:
        self._machine = _load_machine()

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    # ------------------------------------------------------------------ state

    def initial_state(
        self,
        entry_state: str | None = None,
        fixture: dict[str, Any] | None = None,
        config: ModelLaneWorkReducerConfig | None = None,
        *,
        workflow_id: str | None = None,
        change: ModelLaneWorkChange | None = None,
    ) -> ModelLaneWorkReducerState:
        """A state at ``entry_state`` (the contract's initial state by default)."""
        cfg = config or ModelLaneWorkReducerConfig()
        name = entry_state or self._machine.subcontract.initial_state
        if name not in self._machine.states:
            raise ValueError(f"{name!r} is not a state of the contract")
        label = (fixture or {}).get("case", "lane-work")
        plain = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")[:48]
        base = ModelLaneWorkReducerState(
            workflow_id=workflow_id or plain or "lane-work",
            change=change
            or ModelLaneWorkChange(owner="OmniNode-ai", repo="omnimarket", pr_number=1),
            name=name,
            help_tiers=cfg.help_tiers,
            handoff_request_id=(fixture or {}).get("entry_request")
            or ("R1" if fixture else None),
            expected_head=(fixture or {}).get("entry_head"),
        )
        armed, times, counter, _ = self._arm_entry(base, name, None, cfg)
        return base.model_copy(
            update={
                "armed_deadlines": armed,
                "deadline_times": times,
                "arming_counter": counter,
            }
        )

    # ------------------------------------------------------------------- fold

    def handle(self, request: ModelLaneWorkReduceRequest) -> ModelLaneWorkReduceResult:
        return self.reduce(request.state, request.event, request.now, request.config)

    def fold(
        self,
        state: ModelLaneWorkReducerState,
        event: Any,
        now: datetime | str | None = None,
        config: ModelLaneWorkReducerConfig | None = None,
    ) -> tuple[ModelLaneWorkReducerState, list[ModelLaneWorkIntent]]:
        result = self.reduce(state, event, now, config)
        return result.state, list(result.intents)

    def reduce(
        self,
        state: ModelLaneWorkReducerState,
        event: Any,
        now: datetime | str | None = None,
        config: ModelLaneWorkReducerConfig | None = None,
    ) -> ModelLaneWorkReduceResult:
        cfg = config or ModelLaneWorkReducerConfig()
        when = _moment(now)
        incoming = ModelLaneWorkFoldInput.coerce(event)
        if incoming.input_id is not None and incoming.input_id in state.seen_input_ids:
            return self._result("duplicate", state, state)
        if incoming.kind == _STATUS:
            return self._result("status", state, state)

        refusal: str | None = None
        edge: ModelFSMStateTransition | None = None
        if incoming.kind == _REPEAT:
            refusal = "repeat_bound_reached is made by the reducer, never sent in"
        elif (
            incoming.kind == _DEADLINE
            and state.armed_deadlines.get(incoming.deadline or "")
            != incoming.deadline_attempt
        ):
            refusal = (
                f"deadline {incoming.deadline} attempt {incoming.deadline_attempt} "
                "is not the one armed"
            )
        elif (
            incoming.action_id is not None
            and incoming.action_id not in state.outstanding_actions
        ):
            refusal = f"action {incoming.action_id} is not outstanding"
        else:
            edge = self._lookup(state, incoming, when, cfg)
            if (
                edge is None
                and self._machine.states[state.name].state_type == "help"
                and incoming.kind in _HELP_ANSWER_KINDS
            ):
                incoming = incoming.model_copy(update={"kind": "help_failed"})
                edge = self._lookup(state, incoming, when, cfg)
            if edge is None:
                refusal = f"no edge from {state.name} on {incoming.kind}"
        if edge is None:
            return self._refuse(state, incoming, refusal or "refused")

        counters = dict(state.repeat_counters)
        if self._admissible(incoming):
            key = f"{incoming.kind}:{incoming.cls or ''}"
            counters[key] = counters.get(key, 0) + 1
            if counters[key] >= cfg.repeat_bound:
                bound = incoming.model_copy(update={"kind": _REPEAT})
                replacement = self._lookup(state, bound, when, cfg)
                if replacement is not None:
                    incoming, edge = bound, replacement
        if incoming.kind == "proof_passed":
            counters = {}
        return self._apply(state, incoming, edge, when, cfg, counters)

    # ------------------------------------------------------------------ edges

    @staticmethod
    def _admissible(event: ModelLaneWorkFoldInput) -> bool:
        if event.kind == "commit_refused":
            return event.cls == "hook"
        return event.kind in _ALWAYS_ADMISSIBLE

    def _context(
        self,
        state: ModelLaneWorkReducerState,
        event: ModelLaneWorkFoldInput,
        cfg: ModelLaneWorkReducerConfig,
    ) -> dict[str, str]:
        answered = state.handoff_answered_at
        fresh = (
            event.kind in _OBSERVATIONS
            and event.observed_at is not None
            and event.ci_read_at is not None
            and (
                answered is None
                or (event.observed_at > answered and event.ci_read_at > answered)
            )
        )
        flags = {
            "head_matches": _heads_match(event.head_sha, state.expected_head),
            "request_matches": event.request_id is not None
            and event.request_id == state.handoff_request_id,
            "observed_after_answer": fresh,
            "budget_left": "wait_budget" in state.armed_deadlines,
            "help_budget_left": state.help_episodes < cfg.help_budget,
            "tier_is_top": state.help_tier_index >= len(state.help_tiers) - 1,
        }
        context = {name: str(value).lower() for name, value in flags.items()}
        context["event_class"] = event.cls or ""
        context["red_kind"] = event.red_kind or ""
        context["help_origin"] = event.help_origin or ""
        context["deadline"] = event.deadline or ""
        return context

    def _lookup(
        self,
        state: ModelLaneWorkReducerState,
        event: ModelLaneWorkFoldInput,
        when: datetime | None,
        cfg: ModelLaneWorkReducerConfig,
    ) -> ModelFSMStateTransition | None:
        context = self._context(state, event, cfg)
        for edge in self._machine.edges.get((state.name, event.kind), ()):
            if all(
                _holds(condition.expression, context) for condition in edge.conditions
            ):
                return edge
        return None

    # ------------------------------------------------------------------ apply

    def _arm_entry(
        self,
        state: ModelLaneWorkReducerState,
        target: str,
        when: datetime | None,
        cfg: ModelLaneWorkReducerConfig,
    ) -> tuple[dict[str, int], dict[str, str], int, bool]:
        """Deadlines the target state arms, from its entry actions in the contract."""
        armed: dict[str, int] = {}
        times: dict[str, str] = {}
        counter = state.arming_counter
        fresh_once = False

        def arm(deadline: str) -> None:
            nonlocal counter
            counter += 1
            armed[deadline] = counter
            if when is not None:
                seconds = cfg.deadline_seconds[deadline]
                times[deadline] = (when + timedelta(seconds=seconds)).isoformat()

        for entry in self._machine.states[target].entry_actions:
            verb, _, deadline = entry.partition(":")
            carried = deadline in state.armed_deadlines
            if verb == "arm_deadline":
                arm(deadline)
            elif verb == "keep_deadline" and carried:
                armed[deadline] = state.armed_deadlines[deadline]
                if deadline in state.deadline_times:
                    times[deadline] = state.deadline_times[deadline]
            elif verb == "arm_deadline_once":
                if carried:
                    armed[deadline] = state.armed_deadlines[deadline]
                    if deadline in state.deadline_times:
                        times[deadline] = state.deadline_times[deadline]
                else:
                    arm(deadline)
                    fresh_once = True
        return armed, times, counter, fresh_once

    def _apply(
        self,
        state: ModelLaneWorkReducerState,
        event: ModelLaneWorkFoldInput,
        edge: ModelFSMStateTransition,
        when: datetime | None,
        cfg: ModelLaneWorkReducerConfig,
        counters: dict[str, int],
    ) -> ModelLaneWorkReduceResult:
        target = edge.to_state
        definition = self._machine.states[target]
        seq = state.seq + 1
        update: dict[str, Any] = {
            "name": target,
            "seq": seq,
            "repeat_counters": counters,
            "last_refusal": None,
        }
        if event.input_id is not None:
            update["seen_input_ids"] = (*state.seen_input_ids, event.input_id)

        if (event.kind in _HEAD_TAKERS and event.head_sha) or (
            state.expected_head is None
            and event.kind in _OBSERVATIONS
            and event.head_sha
        ):
            update["expected_head"] = event.head_sha
        if event.kind == "start":
            update["worker_tier"] = event.worker_tier
            update["help_tiers"] = cfg.help_tiers
        if event.kind == "worker_started":
            update["worker_lane"] = event.lane_name
        if event.kind == "handoff_written":
            update["handoff_answered_at"] = when
        if event.kind == "proof_passed":
            update["proven_head"] = state.expected_head

        action = edge.actions[0] if edge.actions else None
        config = dict(action.action_config) if action is not None else {}
        enters_help = definition.state_type == "help"
        if enters_help:
            tiers = state.help_tiers
            top = max(len(tiers) - 1, 0)
            if state.name == target:
                update["help_tier_index"] = min(state.help_tier_index + 1, top)
            else:
                names = [tier.name for tier in tiers]
                first = (
                    names.index(state.worker_tier) + 1
                    if state.worker_tier in names
                    else 0
                )
                update["help_tier_index"] = min(first, top)
                update["help_episodes"] = state.help_episodes + 1
                update["help_origin"] = state.name

        armed, times, counter, fresh_once = self._arm_entry(state, target, when, cfg)
        update.update(
            armed_deadlines=armed, deadline_times=times, arming_counter=counter
        )

        outstanding = dict(state.outstanding_actions)
        if event.action_id is not None:
            outstanding.pop(event.action_id, None)

        intents: list[ModelLaneWorkIntent] = []
        interim = state.model_copy(update=update)
        if action is not None:
            action_id = build_action_id(state.workflow_id, seq, 0)
            if action.action_type == "agent":
                kind, actor = "agent", "agent"
            elif action.action_type == "terminal":
                kind, actor = "terminal", "node"
            else:
                kind, actor = action.action_name, "node"
            inputs = {key: value for key, value in config.items() if key != "awaits"}
            inputs.update(
                self._dynamic_inputs(
                    state, interim, event, action.action_name, action_id, fresh_once
                )
            )
            first_deadline = next(iter(times), None)
            intents.append(
                ModelLaneWorkIntent(
                    kind=kind,
                    actor=actor,
                    name=action.action_name,
                    action_id=action_id,
                    inputs=inputs,
                    deadline_at=_moment(times[first_deadline])
                    if first_deadline
                    else None,
                )
            )
            if config.get("awaits") == "true":
                outstanding = {
                    key: value
                    for key, value in outstanding.items()
                    if value != action.action_name
                }
                outstanding[action_id] = action.action_name
                if action.action_name == "handoff_request":
                    update["handoff_request_id"] = action_id
        update["outstanding_actions"] = outstanding
        new_state = state.model_copy(update=update)
        return ModelLaneWorkReduceResult(
            outcome="transition",
            state=new_state,
            intents=tuple(intents),
            from_state=state.name,
            transition_name=edge.transition_name,
        )

    def _dynamic_inputs(
        self,
        before: ModelLaneWorkReducerState,
        after: ModelLaneWorkReducerState,
        event: ModelLaneWorkFoldInput,
        name: str,
        action_id: str,
        fresh_once: bool,
    ) -> dict[str, str]:
        inputs = {
            "workflow_id": after.workflow_id,
            "state": after.name,
            "event": event.kind,
        }
        if after.expected_head:
            inputs["head_sha"] = after.expected_head
        if event.evidence:
            inputs["evidence"] = " | ".join(event.evidence)
        if event.cls:
            inputs["event_class"] = event.cls
        if name == "arm_poll_deadline":
            inputs["wait_budget"] = str(fresh_once).lower()
        if name == "help_request":
            inputs["help_origin"] = after.help_origin or before.name
            if after.help_tiers:
                tier = after.help_tiers[
                    min(after.help_tier_index, len(after.help_tiers) - 1)
                ]
                inputs.update(
                    tier=tier.name,
                    backend_id=tier.backend_id,
                    harness=tier.harness,
                    model=tier.model,
                    effort=tier.effort,
                )
        if name == "handoff_request":
            inputs["expected_head_sha"] = after.expected_head or ""
            inputs["request_id"] = action_id
            if after.change.ticket:
                inputs["claim_ticket"] = after.change.ticket
        if name == "terminal":
            inputs["outcome"] = after.name
            inputs["last_receipt"] = f"{before.workflow_id}-{before.seq}"
            inputs["last_state"] = before.name
            if event.reason:
                inputs["reason"] = event.reason
            if event.decision_needed:
                inputs["decision"] = event.decision_needed
            if after.expected_head:
                inputs["proven_head"] = after.proven_head or after.expected_head
            if before.handoff_request_id:
                inputs["request_id"] = before.handoff_request_id
        return inputs

    # ---------------------------------------------------------------- results

    @staticmethod
    def _result(
        outcome: Literal["transition", "refused", "duplicate", "status"],
        before: ModelLaneWorkReducerState,
        after: ModelLaneWorkReducerState,
        refusal: str | None = None,
    ) -> ModelLaneWorkReduceResult:
        return ModelLaneWorkReduceResult(
            outcome=outcome, state=after, from_state=before.name, refusal=refusal
        )

    def _refuse(
        self,
        state: ModelLaneWorkReducerState,
        event: ModelLaneWorkFoldInput,
        refusal: str,
    ) -> ModelLaneWorkReduceResult:
        update: dict[str, Any] = {"seq": state.seq + 1, "last_refusal": refusal}
        if event.input_id is not None:
            update["seen_input_ids"] = (*state.seen_input_ids, event.input_id)
        return self._result("refused", state, state.model_copy(update=update), refusal)


__all__ = ["HandlerLaneWorkReducer", "deadline_event"]
