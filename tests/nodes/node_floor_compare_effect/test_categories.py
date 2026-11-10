# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23018: the compare page's category roll-up and its paged details.

The grouping is the deployment overlay's, so these tests plant one; a roll-up that gives the right counts for a planted
history, and a details list that is complete and ordered for the page to page through, are the controls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_categories import (
    category_of,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_compare import (
    HandlerFloorCompare,
    build_comparison,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelCompareCategory,
    ModelFloorCompareOverlay,
    ModelFloorCompareRequest,
)
from tests.nodes.node_floor_compare_effect.support import (
    NOW,
    OVERLAY,
    RT,
    build_home,
    seed,
    snap,
)

pytestmark = pytest.mark.unit

PLANTED_THEN = {
    "open_prs": 12,
    "merges": 4,
    "merges_all": 10,
    "red_rows": 5,
    "alarm:deploy": 2,
    "piece:3": 3,
}
PLANTED_NOW = {
    "open_prs": 8,
    "merges": 2,
    "merges_all": 14,
    "red_rows": 5,
    "piece:3": 0,
    "slots_busy": 3,
}


def _planted(overlay: ModelFloorCompareOverlay | None = OVERLAY):
    return build_comparison(
        (snap(1, PLANTED_THEN),),
        snap(0, PLANTED_NOW).model_copy(update={"labels": {"piece:3": "3, Bus"}}),
        RT,
        overlay,
    )


def _day(comparison):
    return next(w for w in comparison.windows if w.days == 1)


def _card(window, card_id: str):
    return next(c for c in window.categories if c.id == card_id)


def test_the_rollup_of_a_planted_history_gives_the_right_counts() -> None:
    day = _day(_planted())
    landing = _card(day, "landing")
    # open_prs 12 to 8 and merges_all 10 to 14 are better, merges 4 to 2 is worse.
    assert (landing.better, landing.worse, landing.same, landing.unread) == (2, 1, 0, 0)
    assert landing.total == 3
    alarms = _card(day, "alarms")
    # red_rows is unchanged; alarm:deploy was read then and not now, so it is unread and not zero.
    assert (alarms.better, alarms.worse, alarms.same, alarms.unread) == (0, 0, 1, 1)
    pieces = _card(day, "pieces")
    assert (pieces.better, pieces.worse, pieces.same, pieces.unread) == (1, 0, 0, 0)
    # slots_busy has a family but no category claims it: it falls to the overlay's other and is not lost.
    other = _card(day, "other")
    assert (other.total, other.unread) == (1, 1)
    assert [c.id for c in day.categories] == ["landing", "alarms", "pieces", "other"]


def test_the_rollup_accounts_for_every_line_of_the_window() -> None:
    for window in _planted().windows:
        in_cards = sum(c.total for c in window.categories)
        assert in_cards == sum(window.counts.values()) == len(window.details)
        for verdict in ("better", "worse", "same", "unread"):
            assert (
                sum(getattr(c, verdict) for c in window.categories)
                == window.counts[verdict]
            )


def test_a_card_says_its_biggest_change_in_one_plain_sentence() -> None:
    landing = _card(_day(_planted()), "landing")
    assert landing.biggest_key == "merges"
    assert (
        landing.biggest_change
        == "Biggest change: In-scope merges down from 4 to 2 (merges in the last hour), which is worse."
    )
    assert landing.sentence.startswith(
        "Landing and merges: 2 better, 1 worse; biggest change:"
    )
    assert _card(_day(_planted()), "pieces").biggest_change == (
        "Biggest change: Rebuild piece 3, Bus went from red to green, which is better."
    )


def test_a_category_with_nothing_read_is_unread_and_says_so() -> None:
    day = _day(_planted())
    other = _card(day, "other")
    assert other.lean == "unread"
    assert "No reading from 1 day ago for any of its 1 number" in other.biggest_change
    empty = _day(build_comparison((), snap(0, {"open_prs": 1}), RT, OVERLAY))
    assert _card(empty, "alarms").lean == "empty"
    assert _card(empty, "alarms").total == 0
    assert _card(empty, "landing").lean == "unread"


def test_the_longest_prefix_wins_between_categories() -> None:
    overlay = ModelFloorCompareOverlay(
        categories=(
            ModelCompareCategory(id="a", title="A", prefixes=("merges",)),
            ModelCompareCategory(id="b", title="B", prefixes=("merges_all",)),
        ),
        other=ModelCompareCategory(id="other", title="Other"),
    )
    assert category_of("merges_all", overlay).id == "b"
    assert category_of("merges", overlay).id == "a"
    assert category_of("nothing", overlay).id == "other"


def test_failing_control_a_prefix_claimed_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match="claimed by two categories"):
        ModelFloorCompareOverlay(
            categories=(
                ModelCompareCategory(id="a", title="A", prefixes=("merges",)),
                ModelCompareCategory(id="b", title="B", prefixes=("merges",)),
            ),
            other=ModelCompareCategory(id="other", title="Other"),
        )
    with pytest.raises(ValidationError, match="ids must be unique"):
        ModelFloorCompareOverlay(
            categories=(ModelCompareCategory(id="other", title="A"),),
            other=ModelCompareCategory(id="other", title="Other"),
        )


def _many(n: int):
    then = {f"git_merges:repo{i:03d}": 10.0 for i in range(n)}
    now = {f"git_merges:repo{i:03d}": 10.0 + (i % 7) - 3 for i in range(n)}
    return build_comparison((snap(1, then),), snap(0, now), RT, OVERLAY)


def test_the_details_hold_every_line_biggest_move_first_for_the_page_to_page() -> None:
    comparison = _many(60)
    assert comparison.details_page_size == 25
    day = _day(comparison)
    # The lists of better and worse lines are capped (the endpoint's old shape); the details are not.
    assert len(day.details) == 60
    assert len(day.better) + len(day.worse) + day.counts["same"] == 60
    sizes = [r.size for r in day.details]
    assert sizes == sorted(sizes, key=lambda x: (x is None, -(x or 0.0)))
    assert -(-len(day.details) // comparison.details_page_size) == 3


def test_details_rows_with_no_reading_are_last_and_have_no_size() -> None:
    rows = _day(_planted()).details
    unread = [r for r in rows if r.verdict == "unread"]
    assert unread
    assert all(r.size is None and r.change_text == "no reading" for r in unread)
    assert rows[-len(unread) :] == tuple(unread)


def test_failing_control_without_the_overlay_there_are_no_cards_and_the_reason_is_in_the_file() -> (
    None
):
    comparison = _planted(None)
    day = _day(comparison)
    assert day.categories == ()
    assert len(day.details) == sum(day.counts.values())
    assert all(r.category == "" for r in day.details)


def _run(home: Path, **kwargs):
    return HandlerFloorCompare(sink=InMemoryEmissionSink(), clock=lambda: NOW).run(
        ModelFloorCompareRequest(home=str(home), now=NOW, **kwargs),
        environ={},
    )


def test_the_cycle_writes_the_rollup_beside_the_old_shape(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    seed(home, snap(1, {"open_prs": 12, "merges": 4}))
    result = _run(home, overlay=OVERLAY)
    assert result.ok
    assert result.categories_error == ""
    doc = json.loads((home / RT.output_relpath).read_text(encoding="utf-8"))
    assert doc["schema_id"] == "omni.floor-compare/1"
    day = doc["windows"][0]
    for old_key in ("better", "worse", "same", "unread", "counts", "label", "days"):
        assert old_key in day
    assert [c["id"] for c in day["categories"]][:1] == ["landing"]
    assert doc["details_page_size"] == 25
    assert doc["categories_error"] == ""
    assert {r["key"] for r in day["details"]} >= {"open_prs", "merges"}


def test_the_cycle_reads_the_grouping_from_the_overlay_root(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    root = tmp_path / "roots" / RT.overlay_id
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(
        yaml.safe_dump(OVERLAY.model_dump(mode="json")), encoding="utf-8"
    )
    result = HandlerFloorCompare(sink=InMemoryEmissionSink(), clock=lambda: NOW).run(
        ModelFloorCompareRequest(home=str(home), now=NOW),
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root.parent)},
    )
    assert result.ok
    assert result.categories_error == ""
    doc = json.loads((home / RT.output_relpath).read_text(encoding="utf-8"))
    assert doc["windows"][0]["categories"]


def test_failing_control_a_missing_or_broken_overlay_keeps_the_history_and_says_why(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    missing = _run(home)
    assert missing.ok
    assert missing.recorded
    assert "overlay" in missing.categories_error
    doc = json.loads((home / RT.output_relpath).read_text(encoding="utf-8"))
    assert doc["categories_error"] == missing.categories_error
    assert doc["windows"][0]["categories"] == []
    assert doc["windows"][0]["details"]

    root = tmp_path / "roots" / RT.overlay_id
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(
        "categories: [not-a-category]\nother: {id: o, title: SECRET-VALUE}\n",
        encoding="utf-8",
    )
    broken = HandlerFloorCompare(sink=InMemoryEmissionSink(), clock=lambda: NOW).run(
        ModelFloorCompareRequest(home=str(home), now=NOW),
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root.parent)},
    )
    assert broken.ok
    assert broken.categories_error
    assert "SECRET-VALUE" not in broken.categories_error
