# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for watchdog consecutive failure alerting."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_process_watchdog.handlers.handler_process_watchdog import (
    ConsecutiveFailurePolicy,
    HandlerProcessWatchdog,
    InmemoryCheckTarget,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_start_command import (
    ModelWatchdogStartCommand,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
    ModelWatchdogCheckResult,
)


def _command(**overrides: object) -> ModelWatchdogStartCommand:
    defaults = {
        "check_targets": [EnumCheckTarget.EMIT_DAEMON],
        "correlation_id": "corr-watchdog",
        "dry_run": True,
        "alert_on_degraded": True,
        "requested_at": datetime.now(tz=UTC),
    }
    defaults.update(overrides)
    return ModelWatchdogStartCommand(**defaults)


def _target(
    name: str,
    status: EnumCheckStatus,
    category: EnumCheckTarget = EnumCheckTarget.EMIT_DAEMON,
) -> InmemoryCheckTarget:
    return InmemoryCheckTarget(
        name=name,
        category=category,
        status=status,
        message=status.value,
    )


@pytest.mark.unit
def test_alert_emitted_only_after_two_consecutive_fails() -> None:
    emitted: list[str] = []
    policy = ConsecutiveFailurePolicy(
        threshold=2,
        emit_alert=lambda result: emitted.append(result.target),
    )
    handler = HandlerProcessWatchdog(alert_policy=policy)
    command = _command(dry_run=False)

    first_report = handler.run_checks(
        command, [_target("emit_daemon", EnumCheckStatus.DOWN)]
    )
    second_report = handler.run_checks(
        command, [_target("emit_daemon", EnumCheckStatus.DOWN)]
    )
    continued_failure_report = handler.run_checks(
        command, [_target("emit_daemon", EnumCheckStatus.DOWN)]
    )
    reset_report = handler.run_checks(
        command, [_target("emit_daemon", EnumCheckStatus.HEALTHY)]
    )
    after_reset_report = handler.run_checks(
        command, [_target("emit_daemon", EnumCheckStatus.DOWN)]
    )
    third_fail_report = handler.run_checks(
        command, [_target("emit_daemon", EnumCheckStatus.DOWN)]
    )

    assert first_report.alerts_emitted == 0
    assert second_report.alerts_emitted == 1
    assert continued_failure_report.alerts_emitted == 0
    assert reset_report.alerts_emitted == 0
    assert after_reset_report.alerts_emitted == 0
    assert third_fail_report.alerts_emitted == 1
    assert emitted == ["emit_daemon", "emit_daemon"]


@pytest.mark.unit
def test_per_target_streak_isolation() -> None:
    emitted: list[str] = []
    policy = ConsecutiveFailurePolicy(
        threshold=2,
        emit_alert=lambda result: emitted.append(result.target),
    )
    handler = HandlerProcessWatchdog(alert_policy=policy)
    command = _command(dry_run=False)

    report_a1 = handler.run_checks(command, [_target("target-a", EnumCheckStatus.DOWN)])
    report_b1 = handler.run_checks(command, [_target("target-b", EnumCheckStatus.DOWN)])
    report_a2 = handler.run_checks(command, [_target("target-a", EnumCheckStatus.DOWN)])
    report_b2 = handler.run_checks(command, [_target("target-b", EnumCheckStatus.DOWN)])

    assert report_a1.alerts_emitted == 0
    assert report_b1.alerts_emitted == 0
    assert report_a2.alerts_emitted == 1
    assert report_b2.alerts_emitted == 1
    assert emitted == ["target-a", "target-b"]


@pytest.mark.unit
def test_same_target_name_isolated_by_category() -> None:
    emitted: list[tuple[EnumCheckTarget, str]] = []
    policy = ConsecutiveFailurePolicy(
        threshold=2,
        emit_alert=lambda result: emitted.append((result.category, result.target)),
    )
    handler = HandlerProcessWatchdog(alert_policy=policy)
    command = _command(
        dry_run=False,
        check_targets=[EnumCheckTarget.EMIT_DAEMON, EnumCheckTarget.UNIX_SOCKET],
    )

    daemon_1 = handler.run_checks(
        command,
        [
            _target(
                "emit_daemon",
                EnumCheckStatus.DOWN,
                category=EnumCheckTarget.EMIT_DAEMON,
            )
        ],
    )
    socket_1 = handler.run_checks(
        command,
        [
            _target(
                "emit_daemon",
                EnumCheckStatus.DOWN,
                category=EnumCheckTarget.UNIX_SOCKET,
            )
        ],
    )
    daemon_2 = handler.run_checks(
        command,
        [
            _target(
                "emit_daemon",
                EnumCheckStatus.DOWN,
                category=EnumCheckTarget.EMIT_DAEMON,
            )
        ],
    )
    socket_2 = handler.run_checks(
        command,
        [
            _target(
                "emit_daemon",
                EnumCheckStatus.DOWN,
                category=EnumCheckTarget.UNIX_SOCKET,
            )
        ],
    )

    assert daemon_1.alerts_emitted == 0
    assert socket_1.alerts_emitted == 0
    assert daemon_2.alerts_emitted == 1
    assert socket_2.alerts_emitted == 1
    assert emitted == [
        (EnumCheckTarget.EMIT_DAEMON, "emit_daemon"),
        (EnumCheckTarget.UNIX_SOCKET, "emit_daemon"),
    ]


@pytest.mark.unit
def test_warn_resets_failure_streak() -> None:
    emitted: list[str] = []
    policy = ConsecutiveFailurePolicy(
        threshold=2,
        emit_alert=lambda result: emitted.append(result.target),
    )
    handler = HandlerProcessWatchdog(alert_policy=policy)
    command = _command(dry_run=False, alert_on_degraded=False)

    handler.run_checks(command, [_target("emit_daemon", EnumCheckStatus.DOWN)])
    warn_report = handler.run_checks(
        command, [_target("emit_daemon", EnumCheckStatus.DEGRADED)]
    )
    after_warn_report = handler.run_checks(
        command, [_target("emit_daemon", EnumCheckStatus.DOWN)]
    )

    assert warn_report.alerts_emitted == 0
    assert after_warn_report.alerts_emitted == 0
    assert emitted == []


@pytest.mark.unit
def test_dry_run_does_not_emit_or_mutate_failure_streak() -> None:
    emitted: list[ModelWatchdogCheckResult] = []
    policy = ConsecutiveFailurePolicy(threshold=2, emit_alert=emitted.append)
    handler = HandlerProcessWatchdog(alert_policy=policy)

    dry_run_report = handler.run_checks(
        _command(dry_run=True),
        [_target("emit_daemon", EnumCheckStatus.DOWN)],
    )
    first_real_report = handler.run_checks(
        _command(dry_run=False),
        [_target("emit_daemon", EnumCheckStatus.DOWN)],
    )
    second_real_report = handler.run_checks(
        _command(dry_run=False),
        [_target("emit_daemon", EnumCheckStatus.DOWN)],
    )

    assert dry_run_report.alerts_emitted == 0
    assert first_real_report.alerts_emitted == 0
    assert second_real_report.alerts_emitted == 1
    assert [result.target for result in emitted] == ["emit_daemon"]
