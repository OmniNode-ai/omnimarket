# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the private-repository hosted-minutes watch (OMN-17427)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from importlib import import_module
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_private_hosted_minutes_watch_effect.handlers import (
    HandlerPrivateHostedMinutesWatch,
)
from omnimarket.nodes.node_private_hosted_minutes_watch_effect.models import (
    ModelHostedMinutesWatchRequest,
    ModelHostedMinutesWatchResult,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_private_hosted_minutes_watch_effect"
)
FIXTURE = (
    Path(__file__).resolve().parent
    / "unit/nodes/node_private_hosted_minutes_watch_effect/fixtures"
    / "billing_usage_month.json"
)
TERMINAL_EVENT = "onex.evt.omnimarket.private-hosted-minutes-watch-completed.v1"
COMMAND_TOPIC = "onex.cmd.omnimarket.private-hosted-minutes-watch-requested.v1"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_contract_names_the_handler_and_its_topics() -> None:
    contract = _contract()
    handler = contract["handler"]
    assert isinstance(handler, dict)
    resolved = getattr(import_module(str(handler["module"])), str(handler["class"]))
    assert resolved is HandlerPrivateHostedMinutesWatch
    assert contract["terminal_event"] == TERMINAL_EVENT
    bus = contract["event_bus"]
    assert isinstance(bus, dict)
    assert bus["publish_topics"] == [TERMINAL_EVENT]
    assert bus["subscribe_topics"] == [COMMAND_TOPIC]


def test_golden_chain_billing_to_alert_and_status_row(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []

    def reader(org: str, year: int, month: int) -> dict[str, object]:
        calls.append(("billing", (org, year, month)))
        return json.loads(FIXTURE.read_text(encoding="utf-8"))

    def sender(text: str) -> str:
        calls.append(("alert", text))
        return "sent"

    def appender(ledger: Path, row: str) -> tuple[bool, str]:
        calls.append(("ledger", row))
        return True, "ledger append exit 0: "

    handler = HandlerPrivateHostedMinutesWatch(
        billing_reader=reader,
        ledger_appender=appender,
        alert_sender=sender,
        clock=lambda: datetime(2026, 10, 19, 6, 0, tzinfo=UTC),
    )
    request = ModelHostedMinutesWatchRequest(
        day=date(2026, 10, 18), state_dir=tmp_path, ledger=tmp_path / "ledger.md"
    )
    result = handler.handle(request)

    assert isinstance(result, ModelHostedMinutesWatchResult)
    assert [kind for kind, _ in calls] == ["billing", "alert", "ledger"]
    assert calls[0] == ("billing", ("OmniNode-ai", 2026, 10))
    assert result.alert == "sent"
    assert result.ledger_written is True
    assert result.total_day_minutes > 0
    assert {row.repository for row in result.day_minutes} >= {"private-repo-a"}
    assert "public-repo-c" not in {row.repository for row in result.day_minutes}
    assert result.status_row.startswith("2026-10-19T06:00:00Z | STATUS | ")
    assert "| alert=sent |" in result.status_row
    heartbeat = json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8"))
    assert heartbeat["day"] == "2026-10-18"
    assert heartbeat["alert"] == "sent"

    again = handler.handle(request)
    assert again.alert == "already-sent"
    assert [kind for kind, _ in calls] == ["billing", "alert", "ledger", "billing"]


def test_error_chain_billing_failure_records_heartbeat(tmp_path: Path) -> None:
    def reader(org: str, year: int, month: int) -> dict[str, object]:
        raise RuntimeError("gh api exit 1: HTTP 403")

    handler = HandlerPrivateHostedMinutesWatch(
        billing_reader=reader,
        ledger_appender=lambda _ledger, _row: (True, ""),
        alert_sender=lambda _text: "sent",
        clock=lambda: datetime(2026, 10, 19, 6, 0, tzinfo=UTC),
    )
    with pytest.raises(RuntimeError, match="HTTP 403"):
        handler.handle(
            ModelHostedMinutesWatchRequest(
                day=date(2026, 10, 18),
                state_dir=tmp_path,
                ledger=tmp_path / "ledger.md",
            )
        )
    heartbeat = json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8"))
    assert heartbeat["error"] == "gh api exit 1: HTTP 403"
