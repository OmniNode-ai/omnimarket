# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fleet projection, alert precedence and idempotence."""

from datetime import timedelta

import pytest

from omnimarket.nodes.node_host_drift_fleet.handlers.fold_host_drift_view import (
    age_text,
    fold_view,
)
from omnimarket.nodes.node_host_drift_fleet.models import (
    EnumHostDriftVerdict,
    ModelHostDriftOpenItem,
)

from .helpers import NOW, heartbeat, overlay, resolved

pytestmark = pytest.mark.unit


def item(
    verdict: EnumHostDriftVerdict, age: int = 60, bound: int = 60
) -> ModelHostDriftOpenItem:
    return ModelHostDriftOpenItem(
        key="unit:service",
        kind="unit",
        declared="running",
        observed="stopped",
        first_seen_at=NOW - timedelta(seconds=age),
        bound_seconds=bound,
        verdict=verdict,
    )


def test_never_reported_stale_and_read_only() -> None:
    view = fold_view(
        previous=None,
        events=(heartbeat(checked_at=NOW - timedelta(seconds=301)),),
        overlay=overlay(
            hosts=("host-a", "host-b", "host-c"), read_only_hosts=("host-c",)
        ),
        now=NOW,
        consumer_group="test",
    )
    assert view.alerts == (
        "host-a heartbeat: heartbeat stale 5m",
        "host-b heartbeat: never reported (age unknown)",
    )
    assert [host.host for host in view.hosts] == ["host-a", "host-b", "host-c"]
    assert "host-c" not in view.status_line


@pytest.mark.parametrize(
    ("verdict", "age", "reason"),
    [
        (EnumHostDriftVerdict.DRIFT, 59, None),
        (EnumHostDriftVerdict.DRIFT, 60, "drift past bound"),
        (EnumHostDriftVerdict.CHECK_FAILED, 0, "check failed"),
        (EnumHostDriftVerdict.OK, 90, None),
    ],
)
def test_drift_bound_and_failed_checks(
    verdict: EnumHostDriftVerdict, age: int, reason: str | None
) -> None:
    view = fold_view(
        previous=None,
        events=(heartbeat(open=(item(verdict, age),)),),
        overlay=overlay(hosts=("host-a",)),
        now=NOW,
        consumer_group="test",
        malformed=7,
    )
    assert view.malformed == 7
    if reason:
        assert len(view.alerts) == 1
        assert reason in view.alerts[0]
    else:
        assert not view.alerts
        within = 1 if verdict == EnumHostDriftVerdict.DRIFT else 0
        assert (
            view.status_line
            == f"host-drift: OK 1/1 hosts reporting, {within} open drift within bound as of 2026-10-31T12:00:00Z"
        )


def test_heartbeat_and_resolution_deduplication_and_bounded_history() -> None:
    events = (
        heartbeat(),
        heartbeat(check_id="older", checked_at=NOW - timedelta(seconds=1)),
        heartbeat(check_id="same-time"),
        *(resolved(check_id=f"check-{n}") for n in range(25)),
    )
    config = overlay(hosts=("host-a",))
    first = fold_view(
        previous=None, events=events, overlay=config, now=NOW, consumer_group="test"
    )
    second = fold_view(
        previous=first, events=events, overlay=config, now=NOW, consumer_group="test"
    )
    assert second == first
    host = second.hosts[0]
    assert host.last_check_id == "check-new"
    assert host.resolved_count == 25
    assert len(host.resolved_ids) == 25
    assert len(host.last_resolved) == 20
    assert host.last_resolved[0].check_id == "check-5"


def test_four_alert_line_keeps_failed_check_and_single_line() -> None:
    hosts = tuple(f"host-{i}" for i in range(6))
    failed = item(EnumHostDriftVerdict.CHECK_FAILED).model_copy(
        update={"observed": "failure\nwith newline"}
    )
    view = fold_view(
        previous=None,
        events=(heartbeat("host-5", open=(failed,)),),
        overlay=overlay(hosts=hosts),
        now=NOW,
        consumer_group="test",
    )
    assert len(view.alerts) == 6
    assert ": check failed," in view.alerts[3]
    assert "host-5 unit:service: check failed" in view.status_line
    assert "host-3 heartbeat" not in view.status_line
    assert "\n" not in view.status_line
    assert "ALERT 6" in view.status_line


def test_heartbeat_at_stale_boundary_and_read_only_observation() -> None:
    view = fold_view(
        previous=None,
        events=(
            heartbeat(checked_at=NOW - timedelta(seconds=300)),
            heartbeat("host-b", open=(item(EnumHostDriftVerdict.CHECK_FAILED),)),
        ),
        overlay=overlay(read_only_hosts=("host-b",)),
        now=NOW,
        consumer_group="test",
    )
    assert not view.alerts
    assert "OK 1/1" in view.status_line
    assert view.hosts[1].open
    assert age_text(3660) == "1h01m"
    assert age_text(-20) == "0m"
