# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane queue starts a waiting lane whenever a host that can take it has a free slot.

Built from the queue as the rolling ledger recorded it on 2026-10-23 (fixtures/lane_queue_2026_10_23.json): nine lanes
queued after ending no-host, 8 free slots over three hosts, one host full. Before this node the throughput tick counted
free slots over the whole pool, so a lane pinned to the full host was relaunched against slots it could not use, and the
runner's own priority holdback counted every queued lane of a higher class as a claimant on a slot that lane could not take.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_lane_queue_start_decision_compute.handlers.handler_lane_queue_start_decision import (
    HandlerLaneQueueStartDecision,
)
from omnimarket.nodes.node_lane_queue_start_decision_compute.models.model_lane_queue_start_decision import (
    ModelLaneQueueStartRequest,
)

pytestmark = pytest.mark.unit

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "lane_queue_2026_10_23.json").read_text(
        encoding="utf-8"
    )
)
HANDLER = HandlerLaneQueueStartDecision()
CLASSES = ["mvp-landing", "occ-retirement", "process-fix", "other"]


def decide(
    entries: list[dict[str, Any]], hosts: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    request = ModelLaneQueueStartRequest.model_validate(
        {
            "now": FIXTURE["now"],
            "priority_classes": CLASSES,
            "entries": entries,
            "hosts": hosts,
            **extra,
        }
    )
    return HANDLER.handle(request).model_dump()


def entry(lane: str, **cells: Any) -> dict[str, Any]:
    base = {
        "lane": lane,
        "model": "sonnet",
        "priority_class": "other",
        "enqueued_at": "2026-10-23T12:00:00Z",
        "updated_at": "2026-10-23T12:00:00Z",
        "pin": None,
        "pin_since": "2026-10-23T12:00:00Z",
        "last_reason": "no host has a free lane slot",
    }
    return {**base, **cells}


def host(name: str, claude: int = 0, codex: int = 0, **cells: Any) -> dict[str, Any]:
    return {"name": name, "claude_slots": claude, "codex_slots": codex, **cells}


def started(result: dict[str, Any]) -> list[str]:
    return [s["lane"] for s in result["starts"]]


def held(result: dict[str, Any]) -> dict[str, str]:
    return {h["lane"]: h["reason_code"] for h in result["held"]}


def test_todays_queue_with_eight_free_slots_starts_eight_lanes_urgent_class_first() -> (
    None
):
    result = decide(FIXTURE["entries"], FIXTURE["hosts"])
    assert started(result) == [
        "lane-queue-start-1400",  # process-fix first
        "consumer-rejoin-1300",
        "capture-watch-1315",  # then the longest wait
        "canon-everywhere-r2-1345",
        "occi-retirement-plan-r5-1310",
        "omniclaude-dev-tag-1345",  # equal waits: by name
        "cmod-ta12-latency-1400",
        "claims-store-pr-1400",
    ]
    assert held(result) == {"m4-c17-c29-diagnose-1410": "no-free-slot"}
    per_host = {
        h: sum(1 for s in result["starts"] if s["host"] == h)
        for h in ("h200", "h201", "h202", "h101")
    }
    assert per_host == {
        "h200": 0,
        "h201": 3,
        "h202": 3,
        "h101": 2,
    }  # a slot is spent on the host that has it
    assert result["depth"] == 9
    assert result["oldest_lane"] == "consumer-rejoin-1300"
    assert result["oldest_wait_s"] == 4325  # 13:00:55Z to 14:13:00Z


def test_an_old_no_free_slot_reason_never_keeps_a_lane_waiting() -> None:
    stale = entry(
        "stale-reason",
        last_reason="engine-pool: no host on the codex hosts has a free slot",
    )
    assert started(decide([stale], [host("h201", claude=1)])) == ["stale-reason"]


def test_a_lane_pinned_to_the_full_host_is_not_started_against_slots_elsewhere() -> (
    None
):
    pinned = entry(
        "other-pinned-h200",
        pin="h200",
        pin_strict=True,
        enqueued_at="2026-10-23T11:55:00Z",
    )
    hosts = [
        host("h200", claude=0),
        host("h201", claude=4),
        host("h202", claude=4),
    ]  # 8 free in the pool, none on h200
    result = decide([pinned], hosts)
    assert started(result) == []
    assert held(result) == {"other-pinned-h200": "pinned-host-full"}
    assert "h200" in result["held"][0]["detail"]
    # the slot opens on h200: the lane starts there, pinned
    freed = decide([pinned], [host("h200", claude=1), *hosts[1:]])
    assert [(s["lane"], s["host"]) for s in freed["starts"]] == [
        ("other-pinned-h200", "h200")
    ]


def test_a_young_pin_holds_and_an_old_pin_is_dropped() -> None:
    young = entry("young", pin="h200", pin_since="2026-10-23T14:05:00Z")
    old = entry(
        "old", pin="h200", pin_since="2026-10-23T13:50:00Z"
    )  # 23 minutes, past the 15-minute pin
    result = decide([young, old], [host("h200"), host("h201", claude=2)])
    assert held(result) == {"young": "pinned-host-full"}
    assert [(s["lane"], s["host"]) for s in result["starts"]] == [("old", "h201")]


def test_a_queued_higher_class_lane_that_cannot_use_the_slot_does_not_hold_it_back() -> (
    None
):
    # h200 has the pool's last free slot; the queued process-fix lane is pinned to h101 (full), so it cannot take it.
    # The runner's holdback counted that lane as waiting and refused the other-class lane for 138 minutes.
    fix = entry(
        "fix-on-h101", priority_class="process-fix", pin="h101", pin_strict=True
    )
    other = entry("other-pinned-h200", pin="h200", pin_strict=True)
    result = decide([fix, other], [host("h200", claude=1), host("h101", claude=0)])
    assert started(result) == ["other-pinned-h200"]
    assert held(result) == {"fix-on-h101": "pinned-host-full"}


def test_a_queued_higher_class_lane_that_can_use_the_slot_takes_it_first() -> None:
    fix = entry(
        "fix-anywhere", priority_class="process-fix", enqueued_at="2026-10-23T13:00:00Z"
    )
    other = entry("other-anywhere", enqueued_at="2026-10-23T11:00:00Z")
    result = decide([other, fix], [host("h200", claude=1)])
    assert started(result) == ["fix-anywhere"]
    assert held(result) == {"other-anywhere": "no-free-slot"}


def test_a_higher_class_lane_waiting_in_its_relaunch_window_keeps_its_share() -> None:
    fix = entry(
        "fix-relaunching",
        priority_class="process-fix",
        launched_at="2026-10-23T14:11:00Z",
        updated_at="2026-10-23T12:00:00Z",
    )
    other = entry("other-lane")
    result = decide([fix, other], [host("h201", claude=1)])
    assert held(result) == {
        "fix-relaunching": "relaunch-window",
        "other-lane": "priority-holdback",
    }
    # with two free slots the fix lane's one share leaves the other lane a slot
    assert started(decide([fix, other], [host("h201", claude=2)])) == ["other-lane"]


def test_an_external_waiting_unit_of_a_higher_class_holds_the_last_slots_but_a_queued_lanes_own_marker_does_not() -> (
    None
):
    other = entry("other-lane")
    hosts = [host("h201", claude=1)]
    external = decide(
        [other],
        hosts,
        waiting_units=[{"unit": "lease-4711", "priority_class": "mvp-landing"}],
    )
    assert held(external) == {"other-lane": "priority-holdback"}
    # the marker the runner wrote for the queued lane itself is not a unit that waits beside the queue
    own = decide(
        [other],
        hosts,
        waiting_units=[{"unit": "lane-other-lane", "priority_class": "other"}],
    )
    assert started(own) == ["other-lane"]
    marker_of_queued_fix = decide(
        [
            entry(
                "fix-pinned-away",
                priority_class="process-fix",
                pin="h101",
                pin_strict=True,
            ),
            other,
        ],
        [host("h201", claude=1), host("h101")],
        waiting_units=[
            {"unit": "lane-fix-pinned-away", "priority_class": "process-fix"}
        ],
    )
    assert started(marker_of_queued_fix) == ["other-lane"]


def test_the_relaunch_window_reopens_after_300_seconds_and_when_the_relaunch_ended_no_host_again() -> (
    None
):
    relaunched = entry(
        "relaunched",
        launched_at="2026-10-23T14:10:30Z",
        updated_at="2026-10-23T12:00:00Z",
    )  # 150 s ago
    assert held(decide([relaunched], [host("h201", claude=1)])) == {
        "relaunched": "relaunch-window"
    }
    reopened = {**relaunched, "launched_at": "2026-10-23T14:07:59Z"}  # 301 s ago
    assert started(decide([reopened], [host("h201", claude=1)])) == ["relaunched"]
    ended_again = {
        **relaunched,
        "updated_at": "2026-10-23T14:10:45Z",
    }  # the relaunch re-enqueued after it was launched
    assert started(decide([ended_again], [host("h201", claude=1)])) == ["relaunched"]
    future = {
        **relaunched,
        "launched_at": "2026-10-23T14:30:00Z",
    }  # a launch stamp ahead of the clock never freezes a lane
    assert started(decide([future], [host("h201", claude=1)])) == ["relaunched"]


def test_codex_lanes_take_codex_slots_and_claude_lanes_claude_slots() -> None:
    lanes = [
        entry("claude-lane"),
        entry("codex-lane", model="codex", enqueued_at="2026-10-23T11:00:00Z"),
    ]
    result = decide(
        lanes, [host("h201", claude=1, codex=0), host("h202", claude=0, codex=1)]
    )
    assert {s["lane"]: s["host"] for s in result["starts"]} == {
        "claude-lane": "h201",
        "codex-lane": "h202",
    }
    only_codex_free = decide([entry("claude-lane")], [host("h201", claude=0, codex=5)])
    assert started(only_codex_free) == []
    assert held(only_codex_free) == {"claude-lane": "no-free-slot"}


def test_an_unread_host_has_no_slots_and_an_unread_pool_says_so() -> None:
    unread = host("h201", claude=4, probe_error="ssh timeout")
    result = decide([entry("lane-a")], [unread])
    assert started(result) == []
    assert held(result) == {"lane-a": "probe-unread"}
    mixed = decide([entry("lane-a")], [unread, host("h202", claude=1)])
    assert [(s["lane"], s["host"]) for s in mixed["starts"]] == [("lane-a", "h202")]


def test_an_empty_queue_starts_and_holds_nothing() -> None:
    result = decide([], [host("h201", claude=3)])
    assert result["starts"] == []
    assert result["held"] == []
    assert result["depth"] == 0
    assert result["oldest_lane"] is None


def test_the_contract_publishes_the_decision_on_its_terminal_event_topic() -> None:
    contract = yaml.safe_load(
        (
            Path(__file__).parents[3]
            / "src/omnimarket/nodes/node_lane_queue_start_decision_compute/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.lane-queue-start-decided.v1"
    ]
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.lane-queue-start-decided.v1"
    )
