# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hand-counted controls for the live compaction signal readers."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from omnibase_core.nodes.node_compute import NodeCompute
from pydantic import ValidationError

from omnimarket.nodes.node_compaction_live_signal_compute.handlers.handler_live_signal import (
    HandlerCompactionLiveSignal,
)
from omnimarket.nodes.node_compaction_live_signal_compute.models.model_live_signal import (
    ModelBoundaryWindow,
    ModelGuardRefusalRow,
    ModelLiveSignalRequest,
    ModelLiveSignalResult,
    ModelOperatorPrompt,
    ModelRulingRow,
)
from omnimarket.nodes.node_compaction_live_signal_compute.node import (
    NodeCompactionLiveSignalCompute,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
NODE = ROOT / "src/omnimarket/nodes/node_compaction_live_signal_compute"

QUOTE = "keep the seed as an accepted exception until the operator says otherwise"
RULING_STAMP = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 1, hour, minute, tzinfo=UTC)


def window(
    window_id: str = "w1", session: str = "s1", start: int = 10, end: int = 12
) -> ModelBoundaryWindow:
    return ModelBoundaryWindow(
        window_id=window_id, session_id=session, start=at(start), end=at(end)
    )


def refusal(
    row_id: str,
    when: datetime,
    guard: str = "question_guard",
    session: str | None = None,
) -> ModelGuardRefusalRow:
    return ModelGuardRefusalRow(
        row_id=row_id, timestamp=when, guard=guard, session_id=session
    )


def prompt(
    prompt_id: str, when: datetime, text: str, session: str = "s1"
) -> ModelOperatorPrompt:
    return ModelOperatorPrompt(
        prompt_id=prompt_id, session_id=session, timestamp=when, text=text
    )


def ruling(
    stamp: datetime = RULING_STAMP,
    quote: str = QUOTE,
    amends: tuple[datetime, ...] = (),
) -> ModelRulingRow:
    return ModelRulingRow(stamp=stamp, quote=quote, amends=amends)


def run(**fields: object) -> ModelLiveSignalResult:
    return HandlerCompactionLiveSignal().handle(ModelLiveSignalRequest(**fields))


def test_fixture_hand_count_two_refusals_and_one_restatement() -> None:
    result = run(
        windows=(window(),),
        guard_rows=(
            refusal("r1", at(10, 30)),
            refusal("r2", at(11, 10)),
            refusal("other", at(11, 0), guard="stop_guard"),
            refusal("late", at(13, 0)),
        ),
        prompts=(
            prompt("p-restated", at(10, 45), f"I said {QUOTE} so stop asking"),
            prompt("p-unrelated", at(11, 0), "please run the focused tests now"),
        ),
        rulings=(ruling(),),
    )
    [w] = result.windows
    assert w.refusal_row_ids == ("r1", "r2")
    assert w.refusal_count == 2
    assert w.duration_hours == 2.0
    assert w.refusals_per_hour == 1.0
    assert w.restatement_count == 1
    assert w.restatements[0].prompt_id == "p-restated"
    assert w.restatements[0].matched_ruling_stamps == (RULING_STAMP,)
    assert w.restatements_per_hour == 0.5
    assert result.question_guard_rows == 3
    assert result.assigned_rows == 2
    assert [(u.row_id, u.reason) for u in result.unassigned] == [
        ("late", "outside_windows")
    ]
    assert result.ignored_other_guard_rows == 1


def test_restatement_followed_by_amending_ruling_within_ten_minutes_not_counted() -> (
    None
):
    amend = ruling(
        stamp=at(11, 35), quote="the seed moves to omnimarket", amends=(RULING_STAMP,)
    )
    result = run(
        windows=(window(),),
        prompts=(prompt("p", at(11, 30), QUOTE),),
        rulings=(ruling(), amend),
    )
    assert result.windows[0].restatement_count == 0


def test_amending_ruling_after_ten_minutes_still_counts() -> None:
    late = ruling(
        stamp=at(11, 41), quote="the seed moves to omnimarket", amends=(RULING_STAMP,)
    )
    result = run(
        windows=(window(),),
        prompts=(prompt("p", at(11, 30), QUOTE),),
        rulings=(ruling(), late),
    )
    assert result.windows[0].restatement_count == 1


def test_amending_ruling_at_exactly_ten_minutes_excuses() -> None:
    edge = ruling(
        stamp=at(11, 40), quote="the seed moves to omnimarket", amends=(RULING_STAMP,)
    )
    result = run(
        windows=(window(),),
        prompts=(prompt("p", at(11, 30), QUOTE),),
        rulings=(ruling(), edge),
    )
    assert result.windows[0].restatement_count == 0


def test_amending_a_different_ruling_does_not_excuse() -> None:
    other = ruling(
        stamp=at(11, 35), quote="something else entirely here", amends=(at(8, 0),)
    )
    result = run(
        windows=(window(),),
        prompts=(prompt("p", at(11, 30), QUOTE),),
        rulings=(ruling(), other),
    )
    assert result.windows[0].restatement_count == 1


def test_overlap_threshold_is_eighty_percent_of_quote_shingles() -> None:
    quote = "one two three four five six seven eight nine"  # 5 shingles
    four_of_five = "one two three four five six seven eight"  # 4 shingles of 5
    three_of_five = "one two three four five six seven"  # 3 shingles of 5
    result = run(
        windows=(window(),),
        prompts=(
            prompt("exact-80", at(10, 30), four_of_five),
            prompt("below-80", at(10, 40), three_of_five),
        ),
        rulings=(ruling(quote=quote),),
    )
    assert [r.prompt_id for r in result.windows[0].restatements] == ["exact-80"]


def test_case_and_punctuation_do_not_hide_a_restatement() -> None:
    result = run(
        windows=(window(),),
        prompts=(prompt("p", at(10, 30), QUOTE.upper().replace(" ", ",  ")),),
        rulings=(ruling(),),
    )
    assert result.windows[0].restatement_count == 1


def test_ruling_not_yet_in_force_at_the_boundary_is_ignored() -> None:
    result = run(
        windows=(window(),),
        prompts=(prompt("p", at(10, 30), QUOTE),),
        rulings=(ruling(stamp=at(10, 10)),),
    )
    assert result.windows[0].restatement_count == 0


def test_short_quote_has_no_shingles_and_never_matches() -> None:
    result = run(
        windows=(window(),),
        prompts=(prompt("p", at(10, 30), "yes go"),),
        rulings=(ruling(quote="yes go"),),
    )
    assert result.windows[0].restatement_count == 0


def test_prompt_counted_once_when_two_rulings_match() -> None:
    twin = ruling(stamp=at(8, 0))
    result = run(
        windows=(window(),),
        prompts=(prompt("p", at(10, 30), QUOTE),),
        rulings=(ruling(), twin),
    )
    [restated] = result.windows[0].restatements
    assert restated.matched_ruling_stamps == (at(8, 0), RULING_STAMP)
    assert result.windows[0].restatement_count == 1


def test_prompt_outside_its_session_window_is_not_counted() -> None:
    result = run(
        windows=(window(),),
        prompts=(
            prompt("other-session", at(10, 30), QUOTE, session="s2"),
            prompt("after-window", at(12, 0), QUOTE),
        ),
        rulings=(ruling(),),
    )
    assert result.windows[0].restatement_count == 0


def test_overlapping_windows_make_a_row_ambiguous_unless_it_names_its_session() -> None:
    result = run(
        windows=(window("w1", "s1"), window("w2", "s2")),
        guard_rows=(
            refusal("blind", at(11, 0)),
            refusal("named", at(11, 5), session="s2"),
        ),
    )
    assert [
        (u.row_id, u.reason, u.candidate_window_ids) for u in result.unassigned
    ] == [("blind", "ambiguous_windows", ("w1", "w2"))]
    by_window = {w.window_id: w.refusal_row_ids for w in result.windows}
    assert by_window == {"w1": (), "w2": ("named",)}


def test_every_question_guard_row_is_assigned_or_unassigned_exactly_once() -> None:
    rows = tuple(
        refusal(f"r{i}", at(9, 0) + timedelta(minutes=17 * i)) for i in range(20)
    )
    result = run(
        windows=(window("w1", "s1", 10, 11), window("w2", "s2", 11, 13)),
        guard_rows=rows,
    )
    assigned = [row_id for w in result.windows for row_id in w.refusal_row_ids]
    unassigned = [u.row_id for u in result.unassigned]
    assert sorted(assigned + unassigned) == sorted(r.row_id for r in rows)
    assert not set(assigned) & set(unassigned)
    assert result.assigned_rows == len(assigned)
    assert result.question_guard_rows == 20


def test_window_end_is_exclusive_and_start_inclusive() -> None:
    result = run(
        windows=(window(),),
        guard_rows=(refusal("start", at(10)), refusal("end", at(12))),
    )
    assert result.windows[0].refusal_row_ids == ("start",)
    assert [u.row_id for u in result.unassigned] == ["end"]


def test_invalid_inputs_fail_loudly() -> None:
    with pytest.raises(ValidationError):
        ModelBoundaryWindow(window_id="w", session_id="s", start=at(12), end=at(10))
    with pytest.raises(ValidationError):
        run(windows=(window("w", "s"), window("w", "s2")))
    with pytest.raises(ValidationError):
        run(
            windows=(window(),), guard_rows=(refusal("a", at(10)), refusal("a", at(11)))
        )
    with pytest.raises(ValidationError):
        ModelGuardRefusalRow(row_id="a", timestamp=datetime(2026, 11, 1, 10), guard="g")


def test_node_contract_and_entry_point() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["runtime_profiles"] == []
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    handler = contract["handler"]
    assert getattr(importlib.import_module(handler["module"]), handler["class"])
    assert issubclass(NodeCompactionLiveSignalCompute, NodeCompute)
    assert any(e.name == NODE.name for e in entry_points(group="onex.nodes"))
