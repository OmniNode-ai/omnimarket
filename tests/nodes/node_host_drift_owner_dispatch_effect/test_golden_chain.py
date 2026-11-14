# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the host drift owner dispatch: open items past the bound start one lane each, once (OMN-24239).

Failing controls: a second pass over the same items starts nothing; an item below the bound starts nothing; a
dry run starts nothing, writes the brief and keeps no record.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelDriftOwnerRequest,
)

from .support import CLAUDE_DIR, RUNNER_PATH, item, overlay, rig

pytestmark = pytest.mark.unit

OLD_ITEM = "env-file:host-env"
OTHER_ITEM = "ledger-writer:bus"
OLD_CAUSE = f"host-drift:h-example:{OLD_ITEM}"
OTHER_CAUSE = f"host-drift:h-example:{OTHER_ITEM}"


def test_two_old_items_start_two_lanes_and_a_second_pass_starts_none() -> None:
    r = rig(
        (
            item(OLD_ITEM),
            item(
                OTHER_ITEM, kind="ledger-writer", first_seen_at="2026-11-09T15:45:26Z"
            ),
        )
    )
    first = r.handler.handle(ModelDriftOwnerRequest(dry_run=False))
    assert first.host == "h-example"
    assert first.items_read == 2
    assert first.rows_rendered == 2
    assert [lane.cause_key for lane in first.lanes] == [OLD_CAUSE, OTHER_CAUSE]
    assert all(lane.started and not lane.dry_run for lane in first.lanes)
    assert len(r.runner.placed) == 2
    assert len(r.briefs.briefs) == 2
    assert set(r.record.record) == {OLD_CAUSE, OTHER_CAUSE}
    # the brief names the cause key, the host and what the checker read
    brief = r.briefs.briefs[f"{first.lanes[0].lane}-brief.md"]
    assert OLD_CAUSE in brief
    assert "h-example" in brief
    assert "1 secret names" in brief

    second = r.handler.handle(ModelDriftOwnerRequest(dry_run=False))
    assert second.lanes == ()
    assert {s.reason for s in second.skipped} == {"already-dispatched"}
    assert len(r.runner.placed) == 2


def test_the_runner_command_is_the_overlays_and_carries_the_cause() -> None:
    r = rig((item(OLD_ITEM),))
    result = r.handler.handle(ModelDriftOwnerRequest(dry_run=False))
    argv = result.lanes[0].argv
    assert argv[:3] == ("python3", RUNNER_PATH, "run")
    assert argv[argv.index("--cause") + 1] == OLD_CAUSE
    assert argv[argv.index("--lane") + 1] == result.lanes[0].lane
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--ticket") + 1] == "OMN-1"
    assert argv[-4:] == ("--parent", "example-drift", "--priority-class", "process-fix")


def test_the_runner_is_given_the_claude_directory_and_each_plugin_before_the_extra_args() -> (
    None
):
    r = rig((item(OLD_ITEM),))
    argv = r.handler.handle(ModelDriftOwnerRequest(dry_run=False)).lanes[0].argv
    assert argv[argv.index("--claude-dir") + 1] == CLAUDE_DIR
    plugins = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--plugin"]
    assert plugins == ["example-plugin@example-marketplace"]
    assert argv.index("--claude-dir") < argv.index("--parent")
    assert argv.index("--plugin") < argv.index("--parent")


def test_an_item_below_the_bound_starts_nothing() -> None:
    r = rig((item(OLD_ITEM, first_seen_at="2026-11-13T15:00:00Z"),))
    result = r.handler.handle(ModelDriftOwnerRequest(dry_run=False))
    assert result.lanes == ()
    assert [s.reason for s in result.skipped] == ["below-bound"]
    assert r.runner.placed == []
    assert r.record.record == {}


def test_a_dry_run_names_the_lane_and_starts_nothing() -> None:
    r = rig((item(OLD_ITEM),))
    result = r.handler.handle(ModelDriftOwnerRequest(dry_run=True))
    (lane,) = result.lanes
    assert lane.dry_run
    assert not lane.started
    assert lane.cause_key == OLD_CAUSE
    assert lane.argv[2] == "run"
    assert r.runner.placed == []
    assert r.record.record == {}
    assert len(r.briefs.briefs) == 1
    # a real pass after the dry run still places the lane
    real = r.handler.handle(ModelDriftOwnerRequest(dry_run=False))
    assert [x.started for x in real.lanes] == [True]


def test_a_lane_that_names_the_cause_covers_it() -> None:
    r = rig((item(OLD_ITEM),), covered_causes=(OLD_CAUSE,))
    result = r.handler.handle(ModelDriftOwnerRequest(dry_run=False))
    assert result.lanes == ()
    assert [s.reason for s in result.skipped] == ["covered-by-cause"]
    assert r.live.asked == [(OLD_CAUSE,)]


def test_an_overlay_naming_no_claude_directory_passes_neither_flag() -> None:
    changed = overlay().model_copy(
        update={
            "dispatch": overlay().dispatch.model_copy(
                update={"claude_dir_env": "", "plugins": ()}
            )
        }
    )
    r = rig((item(OLD_ITEM),), changed=changed)
    argv = r.handler.handle(ModelDriftOwnerRequest(dry_run=False)).lanes[0].argv
    assert "--claude-dir" not in argv
    assert "--plugin" not in argv
