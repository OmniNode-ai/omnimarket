# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm owner responder's golden, bound, coverage, redispatch and purity behaviour (OMN-24200)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_alarm_owner_responder_compute import (
    HandlerAlarmOwnerResponder,
    ModelAlarmOwnerPolicy,
    ModelAlarmOwnerRequest,
)
from omnimarket.nodes.node_stall_responder_compute.handlers.handler_stall_cause_responder import (
    HandlerStallCauseResponder,
    lane_name,
)
from omnimarket.nodes.node_stall_responder_compute.models.model_stall_responder import (
    ModelStallResponderRequest,
)

pytestmark = pytest.mark.unit

CANARY = "delegation-chain-canary:OmniNode-ai/omnibase_infra chain-canary.yml@dev"

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "tests/nodes/node_alarm_owner_responder_compute/fixtures/lab_alarm_friction_rows_2026-11-03.txt"
)
OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_alarm_owner_responder_compute/alarm_owner.overlay.example.yaml"
)


def _rows() -> tuple[str, ...]:
    return tuple(
        line for line in FIXTURE.read_text("utf-8").splitlines() if line.strip()
    )


def _policy() -> ModelAlarmOwnerPolicy:
    loaded = yaml.safe_load(OVERLAY.read_text("utf-8"))
    return ModelAlarmOwnerPolicy.model_validate(loaded["alarm_owner_policy"])


def _request(
    *,
    now: str,
    policy: ModelAlarmOwnerPolicy,
    rows: tuple[str, ...] | None = None,
    dispatched: dict[str, str] | None = None,
    covered_causes: tuple[str, ...] | None = None,
    covered_lanes: tuple[str, ...] | None = None,
) -> ModelAlarmOwnerRequest:
    return ModelAlarmOwnerRequest(
        rows=rows if rows is not None else _rows(),
        now=now,
        policy=policy,
        dispatched=dict(dispatched or {}),
        covered_causes=tuple(covered_causes or ()),
        covered_lanes=tuple(covered_lanes or ()),
    )


def _dispatch_keys(result: Any) -> set[str]:
    return {d.cause_key for d in result.dispatches}


def _expected_keys(rows: tuple[str, ...], policy: ModelAlarmOwnerPolicy) -> set[str]:
    """The cause keys the fixture rows name, computed from the rows, not by hand."""
    keys: set[str] = set()
    for line in rows:
        cells = {
            c.split("=", 1)[0]: c.split("=", 1)[1]
            for c in line.split(" | ")
            if "=" in c
        }
        if cells.get("actor") != policy.alarm_actor:
            continue
        condition = cells.get("condition", "")
        if condition not in policy.conditions:
            continue
        subject = cells.get("subject", "")
        if not subject:
            continue
        cause_class = condition.lower()
        for ch in cause_class:
            if ch not in "abcdefghijklmnopqrstuvwxyz0123456789":
                cause_class = cause_class.replace(ch, "-")
        cause_class = cause_class.strip("-")
        keys.add(f"{cause_class}:{subject}")
    return keys


def test_golden_replay_dispatches_every_policy_condition_key() -> None:
    policy = _policy()
    result = HandlerAlarmOwnerResponder().handle(
        _request(now="2026-11-13T12:00:00Z", policy=policy)
    )
    expected = _expected_keys(_rows(), policy)
    assert CANARY in expected
    assert _dispatch_keys(result) == expected
    canary = next(d for d in result.dispatches if d.cause_key == CANARY)
    assert canary.first_stamp == "2026-11-03T12:52:08Z"
    assert canary.occurrences == 1
    assert canary.ticket == "OMN-20769"
    assert result.outside_conditions >= 2
    assert result.alarm_rows > 0


def test_below_bound_does_not_dispatch_a_key_raised_minutes_ago() -> None:
    """Eight minutes after the chain canary was raised the key is below the bound; older keys still dispatch."""
    policy = _policy()
    result = HandlerAlarmOwnerResponder().handle(
        _request(now="2026-11-03T13:00:00Z", policy=policy)
    )
    assert CANARY not in _dispatch_keys(result)
    assert any(
        s.cause_key == CANARY and s.reason == "below-bound" for s in result.skipped
    )


def test_covered_cause_skips_the_key() -> None:
    policy = _policy()
    key = CANARY
    result = HandlerAlarmOwnerResponder().handle(
        _request(now="2026-11-13T12:00:00Z", policy=policy, covered_causes=(key,))
    )
    assert key not in _dispatch_keys(result)
    assert any(
        s.cause_key == key and s.reason == "covered-by-cause" for s in result.skipped
    )


def test_covered_lane_skips_the_key() -> None:
    policy = _policy()
    key = CANARY
    lane = lane_name(key, "2026-11-12T00:00:00Z")
    result = HandlerAlarmOwnerResponder().handle(
        _request(now="2026-11-13T12:00:00Z", policy=policy, covered_lanes=(lane,))
    )
    assert key not in _dispatch_keys(result)
    assert any(
        s.cause_key == key and s.reason == f"covered:{lane}" for s in result.skipped
    )


def test_umbrella_lane_does_not_cover_the_key() -> None:
    policy = _policy()
    key = CANARY
    result = HandlerAlarmOwnerResponder().handle(
        _request(
            now="2026-11-13T12:00:00Z",
            policy=policy,
            covered_lanes=("orchestrator-31d9",),
        )
    )
    assert key in _dispatch_keys(result)


def test_redispatch_window() -> None:
    policy = _policy()
    key = CANARY
    within = HandlerAlarmOwnerResponder().handle(
        _request(
            now="2026-11-13T12:00:00Z",
            policy=policy,
            dispatched={key: "2026-11-13T10:00:00Z"},
        )
    )
    assert key not in _dispatch_keys(within)
    assert any(
        s.cause_key == key and s.reason == "already-dispatched" for s in within.skipped
    )
    after = HandlerAlarmOwnerResponder().handle(
        _request(
            now="2026-11-14T11:00:00Z",
            policy=policy,
            dispatched={key: "2026-11-13T10:00:00Z"},
        )
    )
    assert key in _dispatch_keys(after)


def test_the_request_is_not_mutated_and_handle_is_pure() -> None:
    policy = _policy()
    dispatched = {CANARY: "2026-11-13T10:00:00Z"}
    request = _request(now="2026-11-13T12:00:00Z", policy=policy, dispatched=dispatched)
    handler = HandlerAlarmOwnerResponder()
    first = handler.handle(request)
    second = handler.handle(request)
    assert first == second
    assert request.dispatched == dispatched


def test_the_old_behaviour_reads_no_alarm_row_and_starts_no_lane() -> None:
    """The stall responder reads only THROUGHPUT STALL MSG rows: over the same recorded rows it starts nothing."""
    request = ModelStallResponderRequest(rows=_rows(), redispatch_after_hours=24.0)
    result = HandlerStallCauseResponder().handle(request)
    assert result.dispatches == ()
    assert result.stall_rows == 0


def test_hourly_ticks_dispatch_the_canary_once_per_window_not_once_per_tick() -> None:
    """Carry the responder's own state tick to tick: one dispatch six hours after the raise, then one a day."""
    policy = _policy()
    rows = _rows()
    dispatched: dict[str, str] = {}
    stamps: list[str] = []
    tick = datetime(2026, 11, 3, 13, 0, 0)
    end = datetime(2026, 11, 6, 13, 0, 0)
    while tick <= end:
        now = tick.strftime("%Y-%m-%dT%H:%M:%SZ")
        seen = tuple(r for r in rows if r[:20] <= now)
        result = HandlerAlarmOwnerResponder().handle(
            _request(now=now, policy=policy, rows=seen, dispatched=dispatched)
        )
        dispatched = result.dispatched
        stamps.extend(now for d in result.dispatches if d.cause_key == CANARY)
        tick += timedelta(hours=1)
    assert stamps == [
        "2026-11-03T19:00:00Z",
        "2026-11-04T19:00:00Z",
        "2026-11-05T19:00:00Z",
    ]
