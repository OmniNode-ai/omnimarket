# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor counts what the landing goal counts (OMN-19998): in-scope merges and open pull requests, the queue age
without paused or out-of-scope pull requests, and how many sit outside.

The error half names its injected breaks: the scope overlay is absent or malformed, and a queue-age run with every
pull request put in scope, which is the break this change removes (it must read the lab as down).
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorSimple,
    ModelGoalScope,
    ModelLabStateOverlay,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    WatcherReading,
    read_queue_age,
    read_watcher,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    OVERLAYS,
    build_home,
    environ,
    overlay_text,
)
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)

pytestmark = pytest.mark.unit

SCOPE = ModelGoalScope(
    ruling="a test goal",
    in_scope_repos=("product",),
    in_scope_tickets=("OMN-22477",),
    in_scope_prs=("r#1", "r#3", "r#9"),
    prerequisite_prs=("r#7",),
    paused_prs=("r#2", "r#6"),
)


def watcher_path(home: Path) -> Path:
    return home / ".claude_scratch/pr-watcher/state.json"


def read(home: Path, scope: ModelGoalScope | None) -> WatcherReading | None:
    return read_watcher(
        watcher_path(home),
        NOW,
        excluded_classes=("held-excluded",),
        queue_classes=("green-unarmed", "queue-stuck"),
        merge_window_minutes=60,
        scope=scope,
    )


def test_golden_the_watcher_counts_in_scope_work_and_the_rest_as_outside(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    reading = read(home, SCOPE)
    assert reading is not None
    assert reading.scoped
    assert (reading.open_prs, reading.merges_in_window) == (2, 2)
    # r#2 (paused), r#4 (unlisted, held) and r#6 (paused) are open and outside the goal.
    assert (reading.open_outside, reading.paused_open) == (3, 2)
    assert reading.merges_outside == 1


def test_golden_the_queue_age_does_not_list_a_paused_pull_request_as_the_lab_down(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    reading = read(home, SCOPE)
    overlay = ModelLabStateOverlay.model_validate(
        overlay_text("node_floor_simple_effect")
    )
    got = read_queue_age(reading, overlay)
    assert got.status == "read"
    # r#3 is the only in-scope green pull request; r#6 waited five hours but is paused.
    assert [s.name for s in got.subjects] == ["r#3"]
    assert all(s.ok for s in got.subjects)
    assert "r#6" not in got.summary


def test_golden_a_kept_ticket_in_the_title_head_or_body_puts_a_pull_request_in_scope() -> (
    None
):
    assert SCOPE.classify("x#1", title="feat(OMN-22477): onboarding") == "in_scope"
    assert SCOPE.classify("x#2", head_ref="mike/omn-22477-walk") == "in_scope"
    assert SCOPE.classify("x#3", body="Closes OMN-22477") == "in_scope"
    assert SCOPE.classify("x#4", body="mentions OMN-22477 only") == "unlisted"
    assert SCOPE.classify("r#2") == "paused"
    assert SCOPE.classify("product#9") == "in_scope"
    # A kept ticket outranks the pause list, as it does for the landing tick.
    assert SCOPE.classify("r#2", title="OMN-22477: x") == "in_scope"


def test_golden_the_page_says_how_many_sit_outside_the_goal(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    run(home)
    doc = written(home)
    outside = doc.outside_goal
    assert outside.read is True
    assert (outside.open_prs, outside.paused_open, outside.merges_in_window) == (
        3,
        2,
        1,
    )
    assert "3 open pull requests" in outside.text
    assert "1 merges" in outside.text
    queue = next(x for x in doc.lab_state if x.id == "queue_age")
    assert queue.state == "WORKING"
    assert "r#6" not in queue.text
    cause_ids = {c.id for c in doc.causes}
    # The red pull request r#2 is paused: only r#1 counts towards the red-checks cause.
    red = next(c for c in doc.causes if c.id == "prs:red")
    assert red.size == 1
    assert "prs:queue-stuck" not in cause_ids


def test_error_without_a_readable_scope_the_numbers_and_queue_age_are_unread_not_everything(
    tmp_path: Path,
) -> None:
    roots = tmp_path / "overlays"
    shutil.copytree(OVERLAYS, roots)
    shutil.rmtree(roots / "goal_scope")
    home = build_home(tmp_path)
    run(home, env=environ(home) | {"ONEX_SKILL_OVERLAY_ROOTS": str(roots)})
    doc: ModelFloorSimple = written(home)
    merges, prs, _slots = doc.numbers
    assert (merges.value, merges.read) == ("unread", False)
    assert (prs.value, prs.read) == ("unread", False)
    queue = next(x for x in doc.lab_state if x.id == "queue_age")
    assert queue.state == "UNKNOWN"
    assert "goal's scope is not read" in queue.text
    assert doc.outside_goal.read is False
    assert "source:goal_scope" in {c.id for c in doc.causes}
    assert doc.level == "red"


def test_error_a_malformed_scope_overlay_is_unread_too(tmp_path: Path) -> None:
    roots = tmp_path / "overlays"
    shutil.copytree(OVERLAYS, roots)
    (roots / "goal_scope" / "overlay.yaml").write_text(
        "in_scope_prs: not-a-list\n", encoding="utf-8"
    )
    home = build_home(tmp_path)
    run(home, env=environ(home) | {"ONEX_SKILL_OVERLAY_ROOTS": str(roots)})
    assert written(home).numbers[1].read is False


def test_error_control_with_every_pull_request_in_scope_the_old_queue_age_reads_the_lab_down(
    tmp_path: Path,
) -> None:
    """The break this change removes: counting r#6 (paused, five hours green) makes the queue age fail."""
    home = build_home(tmp_path)
    everything = SCOPE.model_copy(
        update={"in_scope_prs": (*SCOPE.in_scope_prs, "r#6"), "paused_prs": ()}
    )
    reading = read(home, everything)
    overlay = ModelLabStateOverlay.model_validate(
        overlay_text("node_floor_simple_effect")
    )
    got = read_queue_age(reading, overlay)
    bad = [s for s in got.subjects if not s.ok]
    assert [s.name for s in bad] == ["r#6"]
    assert timedelta(minutes=bad[0].value or 0) > timedelta(minutes=60)
