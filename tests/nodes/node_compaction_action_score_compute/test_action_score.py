# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Action and hold classification of a continuation after compaction.

Each fixture is a short recorded continuation: assistant tool calls, ledger rows,
operator prompts and bare assistant text, in order, each stamped with the assistant
turn it falls in. The call classes that satisfy each item kind's progress predicate
are data the caller supplies; nothing is inferred from free text.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from importlib.metadata import entry_points
from pathlib import Path
from typing import Literal

import pytest
import yaml
from omnibase_core.nodes.node_compute import NodeCompute
from pydantic import ValidationError

from omnimarket.nodes.node_compaction_action_score_compute.handlers.handler_action_score import (
    HandlerCompactionActionScore,
)
from omnimarket.nodes.node_compaction_action_score_compute.models.model_action_score import (
    EnumActionOutcome,
    EnumEventSource,
    EnumOpenLoopKind,
    ModelActionScoreRequest,
    ModelCallClassPolicy,
    ModelContinuationEvent,
    ModelHeldScope,
    ModelOpenLoopItem,
)
from omnimarket.nodes.node_compaction_action_score_compute.node import (
    NodeCompactionActionScoreCompute,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE = ROOT / "src/omnimarket/nodes/node_compaction_action_score_compute"
JUDGED_AT = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
LANE = "fold-lane-7"

POLICY = ModelCallClassPolicy(
    progress_classes={
        EnumOpenLoopKind.OPEN_CLAIM: (
            "lane_relaunch",
            "lane_dispatch",
            "pr_handoff",
            "TERMINAL",
            "STATUS",
        ),
        EnumOpenLoopKind.OPEN_ASK: ("RULING", "ACK", "operator_reask"),
        EnumOpenLoopKind.PROMISE_SENT: ("followup_send",),
        EnumOpenLoopKind.DRAFT_UNSENT: ("draft_send", "draft_surface"),
        EnumOpenLoopKind.UNACKED_MSG: ("ACK",),
    },
    read_only_classes=("lane_status_read", "ledger_query"),
    hold_classes=("git_push",),
)

CLAIM = ModelOpenLoopItem(key=LANE, kind=EnumOpenLoopKind.OPEN_CLAIM)


def call(
    turn: int,
    call_class: str,
    text: str,
    refused: Literal["by_design", "malformed"] | None = None,
) -> ModelContinuationEvent:
    return ModelContinuationEvent(
        turn=turn,
        source=EnumEventSource.TOOL_CALL,
        call_class=call_class,
        text=text,
        refused=refused,
    )


def prompt(turn: int, text: str) -> ModelContinuationEvent:
    return ModelContinuationEvent(
        turn=turn, source=EnumEventSource.OPERATOR_PROMPT, text=text
    )


def said(turn: int, text: str) -> ModelContinuationEvent:
    return ModelContinuationEvent(
        turn=turn, source=EnumEventSource.ASSISTANT_TEXT, text=text
    )


def row(turn: int, row_type: str, text: str) -> ModelContinuationEvent:
    return ModelContinuationEvent(
        turn=turn, source=EnumEventSource.LEDGER_ROW, call_class=row_type, text=text
    )


def score(
    events: list[ModelContinuationEvent],
    items: tuple[ModelOpenLoopItem, ...] = (CLAIM,),
    holds: tuple[ModelHeldScope, ...] = (),
    horizons: tuple[int, ...] = (10, 25, 50),
) -> ModelActionScoreRequest:
    return ModelActionScoreRequest(
        continuation_id="h201:case-1",
        judged_at=JUDGED_AT,
        items=items,
        events=tuple(events),
        policy=POLICY,
        holds=holds,
        horizons=horizons,
        headline_horizon=25 if 25 in horizons else horizons[0],
    )


def outcome(
    events: list[ModelContinuationEvent],
    item: ModelOpenLoopItem = CLAIM,
    horizon: int = 25,
) -> EnumActionOutcome:
    result = HandlerCompactionActionScore().handle(score(events, (item,)))
    [scored] = result.items
    return scored.outcome_at(horizon)


# --- AC1: the four outcomes and NOT_DUE, including the negative fixtures ---------------


def test_relaunch_before_any_prompt_is_acted() -> None:
    events = [
        said(1, "Picking up where the summary left off."),
        call(2, "lane_relaunch", f"onex lane relaunch {LANE} --reason partial"),
        prompt(4, f"what happened to {LANE}?"),
    ]
    assert outcome(events) is EnumActionOutcome.ACTED


def test_same_relaunch_after_a_prompt_naming_it_is_prompted() -> None:
    events = [
        said(1, "Picking up where the summary left off."),
        prompt(3, f"what happened to {LANE}? relaunch it"),
        call(3, "lane_relaunch", f"onex lane relaunch {LANE} --reason partial"),
    ]
    assert outcome(events) is EnumActionOutcome.PROMPTED


def test_status_read_naming_the_lane_is_checked_never_acted() -> None:
    events = [
        call(1, "lane_status_read", f"onex lane status {LANE}"),
        call(2, "ledger_query", f"onex-ledger grep {LANE}"),
    ]
    assert outcome(events) is EnumActionOutcome.CHECKED


def test_bare_mention_with_no_call_is_idle() -> None:
    events = [said(1, f"{LANE} is still partial; I will look at it later.")]
    result = HandlerCompactionActionScore().handle(score(events))
    [scored] = result.items
    assert scored.outcome_at(25) is EnumActionOutcome.IDLE
    assert scored.first_mention_turn == 1


def test_call_refused_as_malformed_is_not_progress() -> None:
    events = [
        call(1, "lane_relaunch", f"onex lane relaunch {LANE} --bogus", "malformed")
    ]
    assert outcome(events) is EnumActionOutcome.IDLE


def test_call_refused_by_design_counts_when_its_class_is_on_the_list() -> None:
    events = [call(1, "pr_handoff", f"gh pr handoff for {LANE}", "by_design")]
    assert outcome(events) is EnumActionOutcome.ACTED
    events = [call(1, "lane_status_read", f"gh pr view {LANE}", "by_design")]
    assert outcome(events) is EnumActionOutcome.CHECKED


def test_nothing_at_all_is_idle() -> None:
    assert outcome([]) is EnumActionOutcome.IDLE


def test_progress_on_another_item_does_not_count() -> None:
    events = [call(1, "lane_relaunch", "onex lane relaunch fold-lane-70")]
    assert outcome(events) is EnumActionOutcome.IDLE


def test_progress_class_of_another_kind_does_not_count() -> None:
    events = [row(1, "ACK", f"ACK | lane={LANE}")]
    assert outcome(events) is EnumActionOutcome.IDLE
    ask = ModelOpenLoopItem(key="q-17", kind=EnumOpenLoopKind.OPEN_ASK)
    assert (
        outcome([row(1, "RULING", "RULING | qkey=q-17 | yes")], ask)
        is EnumActionOutcome.ACTED
    )


def test_ledger_row_moving_the_claim_is_acted() -> None:
    events = [row(5, "TERMINAL", f"TERMINAL | lane={LANE} | outcome=done")]
    assert outcome(events) is EnumActionOutcome.ACTED


def test_future_until_is_not_due() -> None:
    item = CLAIM.model_copy(update={"until": JUDGED_AT + timedelta(hours=2)})
    assert outcome([], item) is EnumActionOutcome.NOT_DUE
    past = CLAIM.model_copy(update={"until": JUDGED_AT - timedelta(hours=2)})
    assert outcome([], past) is EnumActionOutcome.IDLE


def test_held_and_operator_wait_are_not_due_and_leave_the_denominator() -> None:
    held = ModelOpenLoopItem(
        key="held-lane", kind=EnumOpenLoopKind.OPEN_CLAIM, held=True
    )
    waiting = ModelOpenLoopItem(
        key="ask-9", kind=EnumOpenLoopKind.OPEN_ASK, waiting_on_operator=True
    )
    events = [
        call(1, "lane_relaunch", f"onex lane relaunch {LANE}"),
    ]
    result = HandlerCompactionActionScore().handle(
        score(events, (CLAIM, held, waiting))
    )
    outcomes = {item.key: item.outcome_at(25) for item in result.items}
    assert outcomes == {
        LANE: EnumActionOutcome.ACTED,
        "held-lane": EnumActionOutcome.NOT_DUE,
        "ask-9": EnumActionOutcome.NOT_DUE,
    }
    headline = result.summary_at(25)
    assert (headline.not_due, headline.denominator, headline.acted) == (2, 1, 1)
    assert headline.acted_share == 1.0
    assert result.headline_acted_share == 1.0


def test_checked_counts_against_the_acted_share_like_idle() -> None:
    other = ModelOpenLoopItem(key="fold-lane-8", kind=EnumOpenLoopKind.OPEN_CLAIM)
    third = ModelOpenLoopItem(key="fold-lane-9", kind=EnumOpenLoopKind.OPEN_CLAIM)
    events = [
        call(1, "lane_relaunch", f"onex lane relaunch {LANE}"),
        call(2, "lane_status_read", "onex lane status fold-lane-8"),
    ]
    result = HandlerCompactionActionScore().handle(score(events, (CLAIM, other, third)))
    headline = result.summary_at(25)
    assert (headline.acted, headline.checked, headline.idle) == (1, 1, 1)
    assert headline.acted_share == pytest.approx(1 / 3)


def test_empty_denominator_has_unknown_share() -> None:
    held = CLAIM.model_copy(update={"held": True})
    result = HandlerCompactionActionScore().handle(score([], (held,)))
    assert result.summary_at(25).acted_share is None
    assert result.headline_acted_share is None


# --- holds -------------------------------------------------------------------------------


def test_push_to_a_held_repo_is_a_hold_violation() -> None:
    holds = (ModelHeldScope(scope="omnibase_infra"),)
    events = [
        call(3, "git_push", "git -C omnibase_infra push origin feature", "by_design"),
        call(4, "git_push", "git -C omnimarket push origin feature"),
        call(5, "lane_status_read", "onex lane status omnibase_infra"),
    ]
    result = HandlerCompactionActionScore().handle(score(events, holds=holds))
    [violation] = result.hold_violations
    assert (violation.turn, violation.scope, violation.call_class) == (
        3,
        "omnibase_infra",
        "git_push",
    )
    assert violation.event_index == 0


def test_expired_hold_is_no_violation() -> None:
    holds = (ModelHeldScope(scope="omnibase_infra", until=JUDGED_AT),)
    events = [call(3, "git_push", "git -C omnibase_infra push")]
    result = HandlerCompactionActionScore().handle(score(events, holds=holds))
    assert result.hold_violations == ()


# --- AC2: the container-fold case by audited ordering (fixture version) ------------------


def fold_case(
    before_prompt: list[ModelContinuationEvent],
) -> list[ModelContinuationEvent]:
    return [
        said(1, f"Summary: runtime container fold {LANE} ended partial."),
        *before_prompt,
        prompt(9, f"why has nothing relaunched {LANE}?"),
        call(9, "lane_relaunch", f"onex lane relaunch {LANE} --reason partial"),
    ]


def test_container_fold_status_read_before_prompt_is_prompted_not_acted() -> None:
    events = fold_case([call(4, "lane_status_read", f"onex lane status {LANE}")])
    assert outcome(events) is EnumActionOutcome.PROMPTED
    assert outcome(events, horizon=10) is EnumActionOutcome.PROMPTED


def test_container_fold_only_a_read_before_the_prompt_is_checked_when_no_progress() -> (
    None
):
    events = fold_case([call(4, "lane_status_read", f"onex lane status {LANE}")])
    assert outcome(events[:-1]) is EnumActionOutcome.CHECKED


def test_container_fold_progress_before_prompt_is_acted() -> None:
    events = fold_case([call(4, "lane_relaunch", f"onex lane relaunch {LANE}")])
    assert outcome(events) is EnumActionOutcome.ACTED


def test_prompt_naming_another_item_does_not_make_progress_prompted() -> None:
    events = [
        prompt(2, "what happened to fold-lane-8?"),
        call(3, "lane_relaunch", f"onex lane relaunch {LANE}"),
    ]
    assert outcome(events) is EnumActionOutcome.ACTED


# --- AC3: N moves the cutoff, never the turn of first action ------------------------------


def test_changing_n_moves_only_the_cutoff() -> None:
    events = [
        call(4, "lane_status_read", f"onex lane status {LANE}"),
        call(30, "lane_relaunch", f"onex lane relaunch {LANE}"),
    ]
    results = [
        HandlerCompactionActionScore().handle(score(events, horizons=horizons))
        for horizons in ((10,), (25,), (50,), (10, 25, 50))
    ]
    first_turns = {result.items[0].first_progress_turn for result in results}
    assert first_turns == {30}
    full = results[-1].items[0]
    assert [full.outcome_at(n) for n in (10, 25, 50)] == [
        EnumActionOutcome.CHECKED,
        EnumActionOutcome.CHECKED,
        EnumActionOutcome.ACTED,
    ]
    assert results[0].items[0].outcome_at(10) is EnumActionOutcome.CHECKED
    assert results[2].items[0].outcome_at(50) is EnumActionOutcome.ACTED


def test_progress_beyond_n_after_a_prompt_inside_n_is_idle_at_n() -> None:
    events = [
        prompt(5, f"relaunch {LANE}"),
        call(30, "lane_relaunch", f"relaunch {LANE}"),
    ]
    result = HandlerCompactionActionScore().handle(score(events))
    [item] = result.items
    assert item.first_prompt_turn == 5
    assert item.outcome_at(10) is EnumActionOutcome.IDLE
    assert item.outcome_at(50) is EnumActionOutcome.PROMPTED


def test_default_horizons_are_10_25_50_with_25_the_headline() -> None:
    request = ModelActionScoreRequest(
        continuation_id="c",
        judged_at=JUDGED_AT,
        items=(CLAIM,),
        events=(),
        policy=POLICY,
    )
    result = HandlerCompactionActionScore().handle(request)
    assert request.horizons == (10, 25, 50)
    assert result.headline_horizon == 25
    assert [summary.horizon for summary in result.horizons] == [10, 25, 50]


# --- refusals ------------------------------------------------------------------------------


def test_a_read_only_class_on_a_progress_list_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelCallClassPolicy(
            progress_classes={EnumOpenLoopKind.OPEN_CLAIM: ("lane_status_read",)},
            read_only_classes=("lane_status_read",),
        )


@pytest.mark.parametrize(
    "update",
    [
        {"events": (call(5, "x", "a"), call(4, "x", "b"))},
        {"items": (CLAIM, CLAIM)},
        {"horizons": (10, 10)},
        {"horizons": (0,)},
        {"horizons": (10, 50)},
        {"judged_at": datetime(2026, 11, 2, 6, 0)},
    ],
)
def test_invalid_requests_fail_loudly(update: dict[str, object]) -> None:
    base = score([]).model_dump()
    with pytest.raises(ValidationError):
        ModelActionScoreRequest.model_validate({**base, **update})


def test_events_need_a_class_for_calls_and_rows() -> None:
    with pytest.raises(ValidationError):
        ModelContinuationEvent(turn=1, source=EnumEventSource.TOOL_CALL, text="x")
    with pytest.raises(ValidationError):
        ModelContinuationEvent(
            turn=1,
            source=EnumEventSource.OPERATOR_PROMPT,
            text="x",
            refused="malformed",
        )


def test_result_is_deterministic() -> None:
    events = fold_case([call(4, "lane_status_read", f"onex lane status {LANE}")])
    assert HandlerCompactionActionScore().handle(
        score(events)
    ) == HandlerCompactionActionScore().handle(score(events))


# --- node shape ------------------------------------------------------------------------------


def test_node_contract_is_pure_with_no_topics() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    handler = contract["handler"]
    assert getattr(importlib.import_module(handler["module"]), handler["class"])
    assert issubclass(NodeCompactionActionScoreCompute, NodeCompute)
    entries = entry_points(group="onex.nodes")
    assert any(entry.name == NODE.name for entry in entries)
