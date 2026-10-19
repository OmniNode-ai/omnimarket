# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect orchestration uses injected or mocked boundaries only."""

import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnimarket.nodes.node_private_hosted_minutes_watch_effect.handlers import (
    handler_private_hosted_minutes_watch as module,
)
from omnimarket.nodes.node_private_hosted_minutes_watch_effect.models import (
    ModelHostedMinutesWatchRequest,
)

FIXTURES = Path(__file__).parent / "fixtures"
DAY = date(2026, 10, 18)
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def request(tmp_path):
    return ModelHostedMinutesWatchRequest(
        day=DAY, state_dir=tmp_path / "state", ledger=tmp_path / "ledger.md"
    )


def make_handler(fixture="billing_usage_month.json", **overrides):
    reader = Mock(return_value=json.loads((FIXTURES / fixture).read_text()))
    appender = Mock(return_value=(True, "written"))
    sender = Mock(return_value="sent")
    options = {
        "billing_reader": reader,
        "ledger_appender": appender,
        "alert_sender": sender,
        "clock": lambda: NOW,
    }
    options.update(overrides)
    return module.HandlerPrivateHostedMinutesWatch(**options), reader, appender, sender


def test_handle_reads_once_alerts_before_append_and_persists(tmp_path):
    events = []
    handler, reader, appender, sender = make_handler()
    sender.side_effect = lambda text: events.append(("alert", text)) or "sent"
    appender.side_effect = lambda _ledger, row: (
        events.append(("ledger", row)) or (True, "written")
    )
    result = handler.handle(request(tmp_path))
    reader.assert_called_once_with("OmniNode-ai", 2026, 10)
    assert result.day == DAY
    assert result.items_read == 8
    assert result.total_day_minutes == 26.25
    assert [r.repository for r in result.day_minutes] == [
        "private-repo-b",
        "private-repo-a",
    ]
    assert result.month_minutes_by_repo == {
        "private-repo-a": 18.5,
        "private-repo-b": 20.25,
    }
    assert result.daily_totals == {"2026-10-17": 12.5, "2026-10-18": 26.25}
    assert result.alert == "sent"
    assert result.ledger_written is True
    assert events == [("alert", result.alert_text), ("ledger", result.status_row)]
    assert result.status_row.startswith("2026-10-19T12:00:00Z | STATUS |")
    assert "| alert=sent |" in result.status_row
    appender.assert_called_once_with(tmp_path / "ledger.md", result.status_row)
    state = tmp_path / "state"
    record = json.loads((state / "days/2026-10-18.json").read_text())
    assert record == {
        "status_row": result.status_row,
        "ledger_written": True,
        "alert": "sent",
    }
    heartbeat = json.loads((state / "last-run.json").read_text())
    assert heartbeat == {
        "ran_at": "2026-10-19T12:00:00Z",
        "day": "2026-10-18",
        "hosted_minutes": 26.25,
        "alert": "sent",
        "ledger_written": True,
        "items_read": 8,
    }


def test_repeat_day_never_sends_or_appends_again_even_third_run(tmp_path):
    handler, reader, appender, sender = make_handler()
    handler.handle(request(tmp_path))
    for _ in range(2):
        result = handler.handle(request(tmp_path))
        assert result.alert == "already-sent"
        assert result.ledger_written is True
        assert result.ledger_detail == "already written 2026-10-18"
    assert reader.call_count == 3
    appender.assert_called_once()
    sender.assert_called_once()


def test_quiet_day_still_appends_status(tmp_path):
    handler, reader, appender, sender = make_handler("billing_usage_quiet.json")
    result = handler.handle(request(tmp_path))
    reader.assert_called_once_with("OmniNode-ai", 2026, 10)
    appender.assert_called_once()
    sender.assert_not_called()
    assert result.alert == "none"
    assert result.alert_text == ""
    assert result.total_day_minutes == 0
    assert result.day_minutes == ()
    assert "repos=none | alert=none" in result.status_row


def test_ledger_unset_fails_before_billing(tmp_path, monkeypatch):
    monkeypatch.delenv("ONEX_LEDGER_PATH", raising=False)
    handler, reader, appender, sender = make_handler()
    with pytest.raises(ValueError, match="ONEX_LEDGER_PATH is unset"):
        handler.handle(ModelHostedMinutesWatchRequest(day=DAY, state_dir=tmp_path))
    reader.assert_not_called()
    appender.assert_not_called()
    sender.assert_not_called()


def test_ledger_environment_used(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(tmp_path / "env-ledger.md"))
    handler, _, appender, _ = make_handler()
    result = handler.handle(ModelHostedMinutesWatchRequest(day=DAY, state_dir=tmp_path))
    appender.assert_called_once_with(tmp_path / "env-ledger.md", result.status_row)


def test_billing_failure_writes_error_heartbeat(tmp_path):
    reader = Mock(side_effect=RuntimeError("billing unavailable"))
    handler, _, appender, sender = make_handler(billing_reader=reader)
    with pytest.raises(RuntimeError, match="billing unavailable"):
        handler.handle(request(tmp_path))
    assert json.loads((tmp_path / "state/last-run.json").read_text()) == {
        "ran_at": "2026-10-19T12:00:00Z",
        "day": "2026-10-18",
        "error": "billing unavailable",
    }
    reader.assert_called_once_with("OmniNode-ai", 2026, 10)
    appender.assert_not_called()
    sender.assert_not_called()


def test_failed_append_retries_without_resending_alert(tmp_path):
    appender = Mock(side_effect=[(False, "try again"), (True, "written")])
    handler, _, _, sender = make_handler(ledger_appender=appender)
    first = handler.handle(request(tmp_path))
    assert first.ledger_written is False
    assert first.ledger_detail == "try again"
    second = handler.handle(request(tmp_path))
    assert second.ledger_written is True
    assert second.alert == "already-sent"
    assert appender.call_count == 2
    sender.assert_called_once()


def test_unsuccessful_alert_retried_without_second_append(tmp_path):
    sender = Mock(side_effect=["dead", "sent"])
    handler, _, appender, _ = make_handler(alert_sender=sender)
    assert handler.handle(request(tmp_path)).alert == "dead"
    assert handler.handle(request(tmp_path)).alert == "sent"
    assert sender.call_count == 2
    appender.assert_called_once()


def test_equal_day_totals_sort_by_name(tmp_path):
    payload = json.loads((FIXTURES / "billing_usage_month.json").read_text())
    payload["usageItems"][2]["quantity"] = 20.25
    handler, _, _, _ = make_handler(billing_reader=lambda *_: payload)
    assert [r.repository for r in handler.handle(request(tmp_path)).day_minutes] == [
        "private-repo-a",
        "private-repo-b",
    ]


def test_default_billing_reader_one_exact_command(monkeypatch):
    run = Mock(
        return_value=subprocess.CompletedProcess([], 0, '{"usageItems": []}', "")
    )
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.read_billing_usage("OmniNode-ai", 2026, 10) == {"usageItems": []}
    run.assert_called_once()
    args, kwargs = run.call_args
    assert args[0] == [
        "gh",
        "api",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        "/organizations/OmniNode-ai/settings/billing/usage?year=2026&month=10",
    ]
    assert kwargs["env"]["ONEX_GH_READ_ROUTING"] == "0"
    assert kwargs["timeout"] == 120
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


@pytest.mark.parametrize(("rc", "stdout"), [(1, "{}"), (0, "invalid json"), (0, "[]")])
def test_default_billing_errors_include_stderr_tail(monkeypatch, rc, stdout):
    monkeypatch.setattr(
        module.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], rc, stdout, "x" * 350 + "THE END"
            )
        ),
    )
    with pytest.raises(RuntimeError, match="THE END") as error:
        module.read_billing_usage("OmniNode-ai", 2026, 10)
    assert "x" * 301 not in str(error.value)


@pytest.mark.parametrize(
    "failure", [OSError("no gh"), subprocess.TimeoutExpired("gh", 120)]
)
def test_billing_launch_failure_is_runtime_error(monkeypatch, failure):
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=failure))
    with pytest.raises(RuntimeError):
        module.read_billing_usage("OmniNode-ai", 2026, 10)


def test_unconfigured_alert_never_runs_subprocess(monkeypatch):
    monkeypatch.delenv("ONEX_ALERT_CHANNEL_LIB", raising=False)
    run = Mock(side_effect=AssertionError("unexpected process"))
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.send_alert("text") == "unconfigured"
    run.assert_not_called()


@pytest.mark.parametrize(
    ("rc", "expected"), [(0, "sent"), (1, "dead"), (2, "unconfigured"), (3, "failed")]
)
def test_alert_channel_command_and_outcome(monkeypatch, rc, expected):
    monkeypatch.setenv("ONEX_ALERT_CHANNEL_LIB", "/fake/alert-channel.sh")
    monkeypatch.setenv("ONEX_ALERT_CHANNEL_ENV_FILE", "/fake/alerts.env")
    run = Mock(return_value=subprocess.CompletedProcess([], rc, "", ""))
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.send_alert("a text") == expected
    run.assert_called_once_with(
        [
            "bash",
            "-c",
            'set -a; [ -n "$1" ] && [ -r "$1" ] && . "$1" >/dev/null 2>&1; set +a; . "$2" && alert_channel_send "$3" "$4"',
            "_",
            "/fake/alerts.env",
            "/fake/alert-channel.sh",
            "private-hosted-minutes",
            "a text",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    "failure", [OSError("missing"), subprocess.TimeoutExpired("bash", 60)]
)
def test_alert_launch_failure(monkeypatch, failure):
    monkeypatch.setenv("ONEX_ALERT_CHANNEL_LIB", "/fake/alert-channel.sh")
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=failure))
    assert module.send_alert("text") == "failed"


@pytest.mark.parametrize("rc", [0, 4])
def test_ledger_command_and_result(tmp_path, monkeypatch, rc):
    monkeypatch.setenv(
        "ONEX_HOSTED_MINUTES_LEDGER_BIN", 'fake-ledger --config "some path"'
    )
    run = Mock(return_value=subprocess.CompletedProcess([], rc, "", "e" * 210))
    monkeypatch.setattr(module.subprocess, "run", run)
    ledger = tmp_path / "ledger.md"
    assert module.append_ledger(ledger, "row") == (
        rc == 0,
        f"ledger append exit {rc}: " + "e" * 200,
    )
    run.assert_called_once_with(
        ["fake-ledger", "--config", "some path", str(ledger), "--append", "row"],
        capture_output=True,
        text=True,
        timeout=360,
        check=False,
    )


@pytest.mark.parametrize(
    "failure", [OSError("missing"), subprocess.TimeoutExpired("ledger", 360)]
)
def test_ledger_launch_failure(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=failure))
    ok, detail = module.append_ledger(tmp_path / "ledger.md", "row")
    assert ok is False
    assert str(failure) in detail
