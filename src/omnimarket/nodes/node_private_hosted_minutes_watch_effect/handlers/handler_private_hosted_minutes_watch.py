# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read billing once, alert on any billed hosted minute and record the UTC day."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ..models import ModelHostedMinutesWatchRequest, ModelHostedMinutesWatchResult
from ..models.model_hosted_minutes_watch_result import AlertOutcome
from .handler_hosted_minutes_tally import (
    billable_hosted_minutes,
    daily_totals,
    month_by_repo,
    render_alert,
    render_status_row,
)

_ALERT_COMMAND = (
    'set -a; [ -n "$1" ] && [ -r "$1" ] && . "$1" >/dev/null 2>&1; '
    'set +a; . "$2" && alert_channel_send "$3" "$4"'
)


def read_billing_usage(org: str, year: int, month: int) -> dict[str, Any]:
    """Make exactly one GitHub CLI API request, bypassing read routing."""
    try:
        done = subprocess.run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                "X-GitHub-Api-Version: 2022-11-28",
                f"/organizations/{org}/settings/billing/usage?year={year}&month={month}",
            ],
            env={**os.environ, "ONEX_GH_READ_ROUTING": "0"},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"billing read did not run: {exc}") from exc
    if done.returncode != 0:
        raise RuntimeError(f"gh api exit {done.returncode}: {done.stderr[-300:]}")
    try:
        payload = json.loads(done.stdout)
    except ValueError as exc:
        raise RuntimeError(
            f"gh api returned non-JSON output: {done.stderr[-300:]}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"gh api returned a non-object response: {done.stderr[-300:]}"
        )
    return payload


def append_ledger(ledger: Path, row: str) -> tuple[bool, str]:
    """Append through the ledger command, never through a direct file write."""
    argv = shlex.split(os.environ.get("ONEX_HOSTED_MINUTES_LEDGER_BIN", "onex-ledger"))
    try:
        done = subprocess.run(
            [*argv, str(ledger), "--append", row],
            capture_output=True,
            text=True,
            timeout=360,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ledger append did not run: {exc}"
    return (
        done.returncode == 0,
        f"ledger append exit {done.returncode}: {done.stderr[-200:]}",
    )


def send_alert(text: str) -> str:
    """Send once through the operator host's shared alert-channel library."""
    lib = os.environ.get("ONEX_ALERT_CHANNEL_LIB", "").strip()
    if not lib:
        return "unconfigured"
    env_file = os.environ.get("ONEX_ALERT_CHANNEL_ENV_FILE", "").strip()
    try:
        done = subprocess.run(
            [
                "bash",
                "-c",
                _ALERT_COMMAND,
                "_",
                env_file,
                lib,
                "private-hosted-minutes",
                text,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "failed"
    return {0: "sent", 1: "dead", 2: "unconfigured"}.get(done.returncode, "failed")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")


class HandlerPrivateHostedMinutesWatch:
    """One daily watch with independent ledger and successful-alert deduplication."""

    def __init__(
        self,
        *,
        billing_reader: Callable[[str, int, int], dict[str, Any]] | None = None,
        ledger_appender: Callable[[Path, str], tuple[bool, str]] | None = None,
        alert_sender: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._billing_reader = billing_reader or read_billing_usage
        self._ledger_appender = ledger_appender or append_ledger
        self._alert_sender = alert_sender or send_alert
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(
        self, request: ModelHostedMinutesWatchRequest
    ) -> ModelHostedMinutesWatchResult:
        ledger = request.ledger
        if ledger is None:
            ledger_env = os.environ.get("ONEX_LEDGER_PATH", "").strip()
            if not ledger_env:
                raise ValueError("ONEX_LEDGER_PATH is unset: no ledger to record on")
            ledger = Path(ledger_env)
        day = request.day
        stamp = self._clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        heartbeat = request.state_dir / "last-run.json"
        try:
            payload = self._billing_reader(request.org, day.year, day.month)
            items = payload.get("usageItems")
            if not isinstance(items, list):
                raise ValueError("usageItems must be a list")
            rows = billable_hosted_minutes(items)
        except Exception as exc:
            _write_json(
                heartbeat, {"ran_at": stamp, "day": day.isoformat(), "error": str(exc)}
            )
            raise
        day_rows = tuple(
            sorted(
                (row for row in rows if row.day == day and row.minutes > 0),
                key=lambda row: (-row.minutes, row.repository),
            )
        )
        total = sum(row.minutes for row in day_rows)
        month = month_by_repo(rows)
        totals = daily_totals(rows)
        record_path = request.state_dir / "days" / f"{day}.json"
        record = (
            json.loads(record_path.read_text(encoding="utf-8"))
            if record_path.exists()
            else {}
        )
        alert_text = render_alert(request.org, day, day_rows) if total > 0 else ""
        alert: AlertOutcome = "none"
        if record.get("alert") in {"sent", "already-sent"}:
            alert = "already-sent"
        elif total > 0:
            alert = cast(AlertOutcome, self._alert_sender(alert_text))
        row = render_status_row(stamp, day, day_rows, total, alert, len(items), month)
        if record.get("ledger_written") is True:
            ledger_written, ledger_detail = True, f"already written {day}"
        else:
            ledger_written, ledger_detail = self._ledger_appender(ledger, row)
        _write_json(
            record_path,
            {
                "status_row": row,
                "ledger_written": ledger_written,
                # Keep successful delivery durable across any number of repeat runs.
                "alert": "sent" if alert == "already-sent" else alert,
            },
        )
        _write_json(
            heartbeat,
            {
                "ran_at": stamp,
                "day": day.isoformat(),
                "hosted_minutes": total,
                "alert": alert,
                "ledger_written": ledger_written,
                "items_read": len(items),
            },
        )
        return ModelHostedMinutesWatchResult(
            day=day,
            org=request.org,
            items_read=len(items),
            day_minutes=day_rows,
            month_minutes_by_repo=month,
            daily_totals=totals,
            total_day_minutes=total,
            status_row=row,
            ledger_written=ledger_written,
            ledger_detail=ledger_detail,
            alert=alert,
            alert_text=alert_text,
        )
