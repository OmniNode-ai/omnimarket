# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Missing alert-channel credentials are a delivery failure, not a silent return (OMN-20806)."""

from __future__ import annotations

import stat
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.alert_channel_sender import (
    AlertChannelAlarmSender,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.models.model_watchdog_delivery import (
    ModelAlarmSendReceipt,
)
from tests.nodes.node_automation_liveness_watchdog_effect.support import (
    T0,
    FakeChannel,
    FakeEscalation,
    high_alarm,
    make_handler,
    tick,
)

pytestmark = pytest.mark.unit

GOOD_TS = "1760000000.000100"


def _library(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "alert-channel.sh"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _send(path: Path) -> ModelAlarmSendReceipt:
    return AlertChannelAlarmSender(path).send(
        episode_id=high_alarm().episode_id, category="automation-liveness", text="boom"
    )


def test_watchdog_missing_credentials_fail_when_alarm_returns_nonzero(
    tmp_path: Path,
) -> None:
    lib = _library(
        tmp_path, "alert_channel_alarm() { echo 'unresolved' >&2; return 1; }\n"
    )
    receipt = _send(lib)
    assert receipt.delivered is False
    assert receipt.failure is not None
    assert "returned 1" in receipt.failure


def test_watchdog_missing_credentials_fail_when_send_reports_nothing(
    tmp_path: Path,
) -> None:
    """alert_channel_send returns 2 and prints nothing without credentials."""
    lib = _library(tmp_path, "alert_channel_alarm() { return 2; }\n")
    receipt = _send(lib)
    assert receipt.delivered is False
    assert receipt.failure is not None
    assert "returned 2" in receipt.failure


def test_watchdog_missing_credentials_fail_when_success_prints_no_timestamp(
    tmp_path: Path,
) -> None:
    lib = _library(tmp_path, "alert_channel_alarm() { return 0; }\n")
    receipt = _send(lib)
    assert receipt.delivered is False
    assert receipt.failure is not None
    assert "no Slack message timestamp" in receipt.failure


def test_watchdog_missing_credentials_fail_when_library_is_absent(
    tmp_path: Path,
) -> None:
    receipt = AlertChannelAlarmSender(tmp_path / "gone.sh").send(
        episode_id=high_alarm().episode_id, category="c", text="t"
    )
    assert receipt.delivered is False


def test_watchdog_missing_credentials_positive_control_timestamp_is_the_receipt(
    tmp_path: Path,
) -> None:
    lib = _library(
        tmp_path, f"alert_channel_alarm() {{ echo '{GOOD_TS}'; return 0; }}\n"
    )
    receipt = _send(lib)
    assert receipt.delivered is True
    assert receipt.message_ref == GOOD_TS


def test_watchdog_missing_credentials_fail_keeps_the_alarm_pending_and_loud(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    channel.refuse_with = "SLACK_BOT_TOKEN unresolved"
    escalation = FakeEscalation()
    handler = make_handler(tmp_path, channel, escalation)
    alarm = high_alarm()

    first = tick(handler, T0, alarm)
    assert [d.delivered for d in first.delivered] == [False]
    assert first.delivered[0].failure == "SLACK_BOT_TOKEN unresolved"
    assert first.pending == 1

    channel.refuse_with = None
    later = tick(handler, T0 + timedelta(seconds=60))
    assert [d.delivered for d in later.delivered] == [True]
    assert later.pending == 0
