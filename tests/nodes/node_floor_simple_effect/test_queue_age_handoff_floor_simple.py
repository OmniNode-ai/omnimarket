# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The queue age counts from the moment a pull request could be landed (OMN-23056).

A pull request that sat as a draft with green checks and was then handed to landing was read as waiting since its last
check finished: on 2026-11-08 three in-scope pull requests had green checks 53 to 63 minutes old at the 02:12Z tick, were
handed to landing at 02:10Z, and drew lab:queue_age NOT WORKING for a wait of two minutes. The age now starts at the later
of the last finished check and the landing handoff of the same head.

Injected breaks, each a test below: a handoff for another head (a stale one, ignored), and a handoff that is itself old
(the wait is real and must still draw the alarm).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.floor_17a import ModelGoalScope, ModelLabStateOverlay
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    read_queue_age,
    read_watcher,
)
from tests.nodes.node_floor_simple_effect.support import NOW, iso, overlay_text

pytestmark = pytest.mark.unit

SCOPE = ModelGoalScope(ruling="a test goal", in_scope_prs=("r#1",))
HEAD = "a" * 40


def reading_for(
    tmp_path: Path, green_min_ago: int, handoff: dict[str, Any] | None
) -> tuple[int | None, bool]:
    state = {
        "last_tick": iso(NOW - timedelta(minutes=1)),
        "prs": {
            "r#1": {
                "cls": "green-unarmed",
                "ci": {
                    "detail": [["c", "1", iso(NOW - timedelta(minutes=green_min_ago))]]
                },
                "facts": {
                    "state": "OPEN",
                    "draft": False,
                    "title": "x",
                    "head_ref": "x",
                    "head_sha": HEAD,
                },
                "handoff": handoff,
            }
        },
        "merges": {},
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    got = read_watcher(
        path,
        NOW,
        excluded_classes=(),
        queue_classes=("green-unarmed",),
        merge_window_minutes=60,
        scope=SCOPE,
    )
    assert got is not None
    overlay = ModelLabStateOverlay.model_validate(
        overlay_text("node_floor_simple_effect")
    )
    read = read_queue_age(got, overlay)
    return (int(read.subjects[0].value or 0) if read.subjects else None), all(
        s.ok for s in read.subjects
    )


def handoff(minutes_ago: int, head: str = HEAD) -> dict[str, Any]:
    return {"at": iso(NOW - timedelta(minutes=minutes_ago)), "head": head}


def test_golden_a_handoff_after_old_green_checks_starts_the_wait_at_the_handoff(
    tmp_path: Path,
) -> None:
    age, ok = reading_for(tmp_path, green_min_ago=63, handoff=handoff(2))
    assert (age, ok) == (2, True)


def test_golden_without_a_handoff_the_wait_still_starts_at_the_last_check(
    tmp_path: Path,
) -> None:
    age, ok = reading_for(tmp_path, green_min_ago=63, handoff=None)
    assert (age, ok) == (63, False)


def test_error_a_handoff_for_another_head_is_ignored(tmp_path: Path) -> None:
    age, ok = reading_for(tmp_path, green_min_ago=63, handoff=handoff(2, "b" * 40))
    assert (age, ok) == (63, False)


def test_error_a_handoff_that_is_itself_old_still_draws_the_alarm(
    tmp_path: Path,
) -> None:
    age, ok = reading_for(tmp_path, green_min_ago=90, handoff=handoff(75))
    assert (age, ok) == (75, False)


def test_error_a_handoff_older_than_the_last_check_does_not_lengthen_the_wait(
    tmp_path: Path,
) -> None:
    age, ok = reading_for(tmp_path, green_min_ago=5, handoff=handoff(80))
    assert (age, ok) == (5, True)
