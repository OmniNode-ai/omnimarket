"""The stuck plugin version bump judgement reads the PR watcher's records (OMN-20497)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_plugin_bump_stuck_compute.handler import (
    HandlerPluginBumpStuck,
)
from omnimarket.nodes.node_plugin_bump_stuck_compute.models import (
    BUMP_BRANCH,
    ModelPluginBumpStuckRequest,
)

NOW = datetime(2026, 10, 20, 0, 5, tzinfo=UTC)
KEY = "OmniNode-ai/plugin-repo-a#1019"


def watcher(
    minutes_open: float, *, branch: str = BUMP_BRANCH, state: str = "OPEN"
) -> dict:
    opened = (NOW - timedelta(minutes=minutes_open)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "prs": {
            KEY: {
                "facts": {
                    "head_ref": branch,
                    "state": state,
                    "created_at": opened,
                    "head_sha": "a1b2c3d4e5",
                },
                "ci": {"verdict": "red"},
            }
        }
    }


def judge(state):
    return HandlerPluginBumpStuck().handle(ModelPluginBumpStuckRequest(state, NOW))


def test_an_unread_watcher_is_said_not_read_as_no_bump() -> None:
    assert judge(None).lines == ["bump    unread (no fresh PR watcher state)"]


def test_no_open_bump_is_one_line_and_no_reason() -> None:
    result = judge({"prs": {}})
    assert result.lines == ["bump    none open"]
    assert result.reasons == []
    assert result.actions == []


def test_a_young_bump_is_a_line_without_a_reason() -> None:
    result = judge(watcher(30))
    assert result.lines[0].startswith(f"bump    {KEY} open 30m ci=red head=a1b2c3d4")
    assert result.reasons == []


def test_a_bump_past_the_bound_is_a_reason_naming_it_with_one_action() -> None:
    result = judge(watcher(625))
    assert len(result.reasons) == len(result.actions) == 1
    assert KEY in result.reasons[0]
    assert "open 625m (over 90m)" in result.reasons[0]


def test_other_branches_and_closed_bumps_are_not_bumps() -> None:
    assert judge(watcher(625, branch="feature/x")).lines == ["bump    none open"]
    assert judge(watcher(625, state="MERGED")).lines == ["bump    none open"]
