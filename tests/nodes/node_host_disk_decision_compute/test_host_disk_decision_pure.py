# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-23866): the decision is pure, deterministic, and names each command's reason."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.model_host_disk_decision import (
    EnumHostDiskAlarmKind,
    EnumHostDiskReason,
)
from omnimarket.nodes.node_host_disk_decision_compute.handlers.handler_host_disk_decision import (
    HandlerHostDiskDecision,
    decide_host_disk,
)

from .decision_inputs import GIB, NOW, class_reading, group, host_reading, request

pytestmark = pytest.mark.unit

HOST = "example-host-a"


def _uv(*groups):
    return class_reading("uv_cache", tuple(groups))


def test_age_past_keep_days_names_its_reason_and_keeps_the_newest_revision() -> None:
    reading = host_reading(
        HOST,
        classes=(
            _uv(
                group("pkg", "r1", age_days=30),
                group("pkg", "r2", age_days=10),
                group("pkg", "r3", age_days=1),
            ),
        ),
    )
    decision = decide_host_disk(request(reading, host_reading("example-host-b")))
    [command] = decision.commands
    assert command.host_name == HOST
    assert command.class_name == "uv_cache"
    assert command.reason is EnumHostDiskReason.AGE_PAST_KEEP_DAYS
    assert [(t.package, t.revision) for t in command.targets] == [
        ("pkg", "r1"),
        ("pkg", "r2"),
    ]
    assert command.idempotency_key == f"{HOST}|uv_cache|{NOW.isoformat()}"
    assert decision.alarms == ()


def test_the_newest_revision_is_kept_even_when_it_is_older_than_the_window() -> None:
    reading = host_reading(
        HOST,
        classes=(
            _uv(group("old", "r1", age_days=90), group("old", "r0", age_days=95)),
        ),
    )
    decision = decide_host_disk(request(reading, host_reading("example-host-b")))
    [command] = decision.commands
    assert [(t.package, t.revision) for t in command.targets] == [("old", "r0")]


def test_a_revision_a_live_lane_holds_is_never_a_target() -> None:
    reading = host_reading(
        HOST,
        classes=(
            _uv(
                group("pkg", "r1", age_days=30, held=1),
                group("pkg", "r2", age_days=20),
                group("pkg", "r3", age_days=1),
            ),
        ),
    )
    decision = decide_host_disk(request(reading, host_reading("example-host-b")))
    [command] = decision.commands
    assert [t.revision for t in command.targets] == ["r2"]


def test_class_over_cap_evicts_oldest_first_until_under_the_cap() -> None:
    # example policy: uv_cache cap 50 GiB, keep 7 days. Everything is young, so only the cap acts.
    groups = [group("big", f"r{i}", age_days=i + 1, bytes_=20 * GIB) for i in range(4)]
    groups.append(group("other", "r0", age_days=0.5, bytes_=20 * GIB))
    reading = host_reading(HOST, classes=(_uv(*groups),))
    decision = decide_host_disk(request(reading, host_reading("example-host-b")))
    [command] = decision.commands
    assert command.reason is EnumHostDiskReason.CLASS_OVER_CAP
    # 100 GiB over a 50 GiB cap: three 20 GiB groups must go (two leave 60 GiB), the oldest unprotected first.
    assert [t.revision for t in command.targets] == ["r1", "r2", "r3"]
    assert {t.reason for t in command.targets} == {EnumHostDiskReason.CLASS_OVER_CAP}


def test_under_floor_shortens_the_window_and_alarms() -> None:
    reading = host_reading(
        HOST,
        free_bytes=10 * GIB,
        classes=(
            _uv(group("pkg", "r1", age_days=3), group("pkg", "r2", age_days=0.1)),
        ),
    )
    decision = decide_host_disk(request(reading, host_reading("example-host-b")))
    [command] = decision.commands
    assert command.reason is EnumHostDiskReason.HOST_UNDER_FLOOR
    assert [t.revision for t in command.targets] == ["r1"]
    [alarm] = decision.alarms
    assert alarm.kind is EnumHostDiskAlarmKind.UNDER_FLOOR
    assert alarm.free_bytes == 10 * GIB
    assert alarm.floor_free_bytes == 100 * GIB


def test_a_stale_reading_alarms_and_decides_no_command() -> None:
    stale = host_reading(
        HOST,
        read_at=NOW - timedelta(hours=3),
        classes=(_uv(group("pkg", "r1", age_days=30), group("pkg", "r2", age_days=1)),),
    )
    decision = decide_host_disk(request(stale, host_reading("example-host-b")))
    assert decision.commands == ()
    [alarm] = decision.alarms
    assert alarm.kind is EnumHostDiskAlarmKind.STALE_READING
    assert alarm.host_name == HOST


def test_a_host_the_policy_expects_with_no_reading_alarms() -> None:
    decision = decide_host_disk(request(host_reading("example-host-b")))
    [alarm] = decision.alarms
    assert alarm.kind is EnumHostDiskAlarmKind.MISSING_READING
    assert alarm.host_name == HOST


def test_the_same_request_yields_the_same_decision_in_any_input_order() -> None:
    a = host_reading(
        HOST,
        classes=(_uv(group("p", "r1", age_days=30), group("p", "r2", age_days=1)),),
    )
    b = host_reading(
        "example-host-b",
        free_bytes=1 * GIB,
        classes=(_uv(group("q", "r1", age_days=9), group("q", "r2", age_days=1)),),
    )
    first = decide_host_disk(request(a, b))
    assert decide_host_disk(request(b, a)) == first
    assert HandlerHostDiskDecision().handle(request(a, b)) == first
    assert first.model_dump_json() == decide_host_disk(request(a, b)).model_dump_json()


def test_unread_classes_and_classes_without_a_policy_decide_nothing() -> None:
    unread = class_reading("uv_cache", ()).model_copy(
        update={"status": "unread", "unread_reason": "permission denied"}
    )
    other = class_reading(
        "lab_run", (group("x", "r1", age_days=400), group("x", "r2", age_days=500))
    )
    reading = host_reading(HOST, classes=(unread, other))
    decision = decide_host_disk(request(reading, host_reading("example-host-b")))
    assert decision.commands == ()
