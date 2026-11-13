# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A repeating install failure raises after N rows and again after its limit (OMN-21663).

The recorded case: the landing controller's own update failed 207 times (rows of its ``install-failures.jsonl``)
with no alarm. The decision is pure: the caller reads the rows and passes the bounds from its overlay.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_silent_failure_alarm_compute.handlers.handler_install_failure_alarm import (
    HandlerInstallFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.models.model_install_failure_alarm import (
    ModelInstallFailureAlarmRequest,
    ModelInstallFailureAlarmResult,
    ModelInstallFailureRow,
    ModelInstallFailureState,
)

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _rows(*minutes_ago: int) -> tuple[ModelInstallFailureRow, ...]:
    return tuple(
        ModelInstallFailureRow(
            at=NOW - timedelta(minutes=m), detail=f"installer exit 1 at {m}"
        )
        for m in minutes_ago
    )


def _decide(
    rows: tuple[ModelInstallFailureRow, ...],
    previous: ModelInstallFailureState | None = None,
    *,
    threshold: int = 3,
    window: int = 360,
    reraise: int = 240,
    readable: bool = True,
) -> ModelInstallFailureAlarmResult:
    return HandlerInstallFailureAlarm().handle(
        ModelInstallFailureAlarmRequest(
            now=NOW,
            readable=readable,
            failures=rows,
            threshold=threshold,
            window_minutes=window,
            reraise_after_minutes=reraise,
            previous=previous or ModelInstallFailureState(),
        )
    )


def test_install_failure_below_the_threshold_is_ok() -> None:
    result = _decide(_rows(10, 20))
    assert result.verdict == "OK"
    assert result.count == 2
    assert not result.alert_text


def test_install_failure_at_the_threshold_raises_with_count_and_age() -> None:
    result = _decide(_rows(5, 65, 125))
    assert result.verdict == "RAISE"
    assert result.count == 3
    assert result.raise_count == 1
    assert "INSTALL FAILING 3 times" in result.alert_text[0]
    assert "125 minutes" in result.alert_text[0]
    assert "installer exit 1 at 5" in result.alert_text[0]
    assert result.state.raised_at == NOW


def test_install_failure_rows_outside_the_window_do_not_count() -> None:
    result = _decide(_rows(5, 65, 400, 500, 600))
    assert result.count == 2
    assert result.verdict == "OK"


def test_install_failure_held_inside_the_reraise_limit_and_raised_again_past_it() -> (
    None
):
    raised = ModelInstallFailureState(
        raised_at=NOW - timedelta(minutes=100), raise_count=1
    )
    held = _decide(_rows(5, 65, 125), raised)
    assert held.verdict == "HELD"
    assert not held.alert_text
    old = ModelInstallFailureState(
        raised_at=NOW - timedelta(minutes=250), raise_count=1
    )
    again = _decide(_rows(5, 65, 125), old)
    assert again.verdict == "RAISE"
    assert again.raise_count == 2
    assert "raised 2 times" in again.alert_text[0]


def test_install_failure_an_ok_reading_forgets_the_last_raise() -> None:
    old = ModelInstallFailureState(
        raised_at=NOW - timedelta(minutes=250), raise_count=4
    )
    result = _decide(_rows(5), old)
    assert result.verdict == "OK"
    assert result.state == ModelInstallFailureState()


def test_install_failure_threshold_and_window_are_the_callers_values() -> None:
    assert _decide(_rows(5, 65), threshold=2).verdict == "RAISE"
    assert _decide(_rows(5, 65, 125), window=100).verdict == "OK"


def test_install_failure_unreadable_rows_are_never_an_ok_verdict() -> None:
    result = _decide((), readable=False)
    assert result.verdict == "UNREADABLE"
