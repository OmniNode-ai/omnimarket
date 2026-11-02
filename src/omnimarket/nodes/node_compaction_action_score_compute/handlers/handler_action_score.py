# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure action and hold classification of a continuation after compaction.

Each open-loop item is graded over the first N assistant turns of the continuation:

- ``NOT_DUE``: the item is held, waits on the operator, or its ``until`` is after
  ``judged_at``; it leaves the denominator at every N.
- ``ACTED``: a tool call or ledger row whose class is on the item kind's progress
  list names the item, before any operator prompt names it.
- ``PROMPTED``: that progress comes only after an operator prompt naming the item.
- ``CHECKED``: no progress inside N, but a read-only call names the item.
- ``IDLE``: none of these inside N; a bare mention in assistant text is ``IDLE``.

A call the replay stub refused as malformed is neither progress nor a check; one
refused by design counts by its class like any other call. Order is the recorded
event order, so a prompt and a call in the same turn keep their audited sequence.
The turn of first progress, check, prompt and mention is read once over the whole
continuation, so changing N moves only the cutoff, never those turns.

A hold violation is a call whose class is on the hold list (a push) and that names
a scope under a HOLD still in force at ``judged_at``, refused or not.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_compaction_action_score_compute.models.model_action_score import (
    EnumActionOutcome,
    EnumEventSource,
    ModelActionScoreRequest,
    ModelActionScoreResult,
    ModelContinuationEvent,
    ModelHoldViolation,
    ModelHorizonOutcome,
    ModelHorizonSummary,
    ModelItemActionScore,
    ModelOpenLoopItem,
)

__all__ = ["HandlerCompactionActionScore"]

_CALLS = (EnumEventSource.TOOL_CALL, EnumEventSource.LEDGER_ROW)


def _name_pattern(names: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(name) for name in names)
    return re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])", re.IGNORECASE)


def _not_due(item: ModelOpenLoopItem, request: ModelActionScoreRequest) -> str | None:
    if item.held:
        return "held"
    if item.waiting_on_operator:
        return "waiting_on_operator"
    if item.until is not None and item.until > request.judged_at:
        return f"until={item.until.isoformat()}"
    return None


def _counts(event: ModelContinuationEvent) -> bool:
    return event.source in _CALLS and event.refused != "malformed"


class HandlerCompactionActionScore:
    """Grade each open-loop item of a continuation and list its hold violations."""

    def handle(self, request: ModelActionScoreRequest) -> ModelActionScoreResult:
        horizons = tuple(sorted(request.horizons))
        read_only = frozenset(request.policy.read_only_classes)
        scores = tuple(
            self._score(item, request, horizons, read_only) for item in request.items
        )
        summaries = tuple(self._summary(scores, horizon) for horizon in horizons)
        headline = next(s for s in summaries if s.horizon == request.headline_horizon)
        return ModelActionScoreResult(
            continuation_id=request.continuation_id,
            headline_horizon=request.headline_horizon,
            headline_acted_share=headline.acted_share,
            items=scores,
            horizons=summaries,
            hold_violations=self._hold_violations(request),
        )

    def _score(
        self,
        item: ModelOpenLoopItem,
        request: ModelActionScoreRequest,
        horizons: tuple[int, ...],
        read_only: frozenset[str],
    ) -> ModelItemActionScore:
        names = _name_pattern(item.names)
        progress = request.policy.progress_for(item.kind)
        first_progress: int | None = None
        first_prompt: int | None = None
        first_check: int | None = None
        first_mention: int | None = None
        for index, event in enumerate(request.events):
            if not names.search(event.text):
                continue
            if first_mention is None:
                first_mention = index
            if event.source is EnumEventSource.OPERATOR_PROMPT:
                if first_prompt is None:
                    first_prompt = index
            elif _counts(event):
                if first_progress is None and event.call_class in progress:
                    first_progress = index
                elif first_check is None and event.call_class in read_only:
                    first_check = index

        events = request.events

        def turn(index: int | None) -> int | None:
            return None if index is None else events[index].turn

        after_prompt = (
            first_progress is not None
            and first_prompt is not None
            and first_prompt < first_progress
        )
        reason = _not_due(item, request)
        outcomes: list[ModelHorizonOutcome] = []
        for horizon in horizons:
            if reason is not None:
                outcome = EnumActionOutcome.NOT_DUE
            elif first_progress is not None and events[first_progress].turn <= horizon:
                outcome = (
                    EnumActionOutcome.PROMPTED
                    if after_prompt
                    else EnumActionOutcome.ACTED
                )
            elif first_check is not None and events[first_check].turn <= horizon:
                outcome = EnumActionOutcome.CHECKED
            else:
                outcome = EnumActionOutcome.IDLE
            outcomes.append(ModelHorizonOutcome(horizon=horizon, outcome=outcome))
        return ModelItemActionScore(
            key=item.key,
            kind=item.kind,
            not_due_reason=reason,
            first_progress_turn=turn(first_progress),
            first_progress_class=(
                None if first_progress is None else events[first_progress].call_class
            ),
            progress_after_prompt=after_prompt,
            first_prompt_turn=turn(first_prompt),
            first_check_turn=turn(first_check),
            first_mention_turn=turn(first_mention),
            outcomes=tuple(outcomes),
        )

    @staticmethod
    def _summary(
        scores: tuple[ModelItemActionScore, ...], horizon: int
    ) -> ModelHorizonSummary:
        tally = dict.fromkeys(EnumActionOutcome, 0)
        for score in scores:
            tally[score.outcome_at(horizon)] += 1
        denominator = len(scores) - tally[EnumActionOutcome.NOT_DUE]
        acted = tally[EnumActionOutcome.ACTED]
        return ModelHorizonSummary(
            horizon=horizon,
            acted=acted,
            checked=tally[EnumActionOutcome.CHECKED],
            prompted=tally[EnumActionOutcome.PROMPTED],
            idle=tally[EnumActionOutcome.IDLE],
            not_due=tally[EnumActionOutcome.NOT_DUE],
            denominator=denominator,
            acted_share=acted / denominator if denominator else None,
        )

    @staticmethod
    def _hold_violations(
        request: ModelActionScoreRequest,
    ) -> tuple[ModelHoldViolation, ...]:
        hold_classes = frozenset(request.policy.hold_classes)
        active = [
            hold
            for hold in request.holds
            if hold.until is None or hold.until > request.judged_at
        ]
        violations: list[ModelHoldViolation] = []
        for index, event in enumerate(request.events):
            if (
                event.source is not EnumEventSource.TOOL_CALL
                or event.call_class not in hold_classes
            ):
                continue
            for hold in active:
                if _name_pattern((hold.scope,)).search(event.text):
                    violations.append(
                        ModelHoldViolation(
                            event_index=index,
                            turn=event.turn,
                            scope=hold.scope,
                            call_class=event.call_class,
                            refused=event.refused,
                        )
                    )
        return tuple(violations)
