# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI health statuses and errors without any external effects."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnimarket.nodes.node_private_hosted_minutes_watch_effect import __main__ as cli
from omnimarket.nodes.node_private_hosted_minutes_watch_effect.handlers import (
    handler_private_hosted_minutes_watch as effects,
)

FIXTURES = Path(__file__).parent / "fixtures"


def install_defaults(
    monkeypatch, *, alert="sent", written=True, failure=False, quiet=False
):
    payload = json.loads(
        (
            FIXTURES
            / ("billing_usage_quiet.json" if quiet else "billing_usage_month.json")
        ).read_text()
    )
    reader = Mock(
        return_value=payload,
        side_effect=RuntimeError("billing unavailable") if failure else None,
    )
    monkeypatch.setattr(effects, "read_billing_usage", reader)
    monkeypatch.setattr(
        effects, "append_ledger", Mock(return_value=(written, "fake ledger"))
    )
    monkeypatch.setattr(effects, "send_alert", Mock(return_value=alert))
    return reader


def args(tmp_path):
    return [
        "--day",
        "2026-10-18",
        "--state-dir",
        str(tmp_path / "state"),
        "--ledger",
        str(tmp_path / "ledger.md"),
        "--omni-home",
        str(tmp_path),
    ]


@pytest.mark.parametrize(
    ("alert", "written", "expected"),
    [
        ("sent", True, 0),
        ("already-sent", True, 0),
        ("dead", True, 1),
        ("unconfigured", True, 1),
        ("failed", True, 1),
        ("sent", False, 1),
    ],
)
def test_cli_health_and_summary(
    tmp_path, monkeypatch, capsys, alert, written, expected
):
    reader = install_defaults(monkeypatch, alert=alert, written=written)
    assert cli.main(args(tmp_path)) == expected
    reader.assert_called_once_with("OmniNode-ai", 2026, 10)
    output = capsys.readouterr().out.splitlines()
    assert output == [
        f"private-hosted-minutes-watch: day=2026-10-18 hosted_minutes=26.25 repos=2 alert={alert} ledger_written={written}",
        "  private-repo-b: 20.25 min",
        "  private-repo-a: 6 min",
    ]


def test_cli_quiet_day_healthy(tmp_path, monkeypatch, capsys):
    install_defaults(monkeypatch, quiet=True)
    assert cli.main(args(tmp_path)) == 0
    assert (
        "hosted_minutes=0 repos=0 alert=none ledger_written=True"
        in capsys.readouterr().out
    )


def test_cli_billing_failure_writes_heartbeat_and_exits_two(
    tmp_path, monkeypatch, capsys
):
    install_defaults(monkeypatch, failure=True)
    assert cli.main(args(tmp_path)) == 2
    assert (
        capsys.readouterr().err
        == "private-hosted-minutes-watch: billing read failed: billing unavailable\n"
    )
    assert (
        json.loads((tmp_path / "state/last-run.json").read_text())["error"]
        == "billing unavailable"
    )


def test_cli_unconfigured_alert_without_library(tmp_path, monkeypatch):
    sender = effects.send_alert
    install_defaults(monkeypatch)
    monkeypatch.setattr(effects, "send_alert", sender)
    monkeypatch.delenv("ONEX_ALERT_CHANNEL_LIB", raising=False)
    assert cli.main(args(tmp_path)) == 1


def test_cli_default_day_is_yesterday_utc(tmp_path, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return cls(2026, 10, 19, 0, 1, tzinfo=UTC)

    monkeypatch.setattr(cli, "datetime", FixedDatetime)
    reader = install_defaults(monkeypatch, quiet=True)
    assert cli.main(args(tmp_path)[2:]) == 0
    reader.assert_called_once_with("OmniNode-ai", 2026, 10)
    assert (tmp_path / "state/days/2026-10-18.json").exists()


def test_cli_invalid_date_rejected_before_billing(tmp_path, monkeypatch):
    reader = install_defaults(monkeypatch)
    with pytest.raises(SystemExit) as error:
        cli.main(["--day", "not-a-date", *args(tmp_path)[2:]])
    assert error.value.code == 2
    reader.assert_not_called()
