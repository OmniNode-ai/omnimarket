# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded-row builders and a recording publisher for the work progress alert tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_work_progress_alert_effect.handlers import (
    handler_work_progress_alert,
)
from omnimarket.nodes.node_work_progress_alert_effect.models import ModelLedgerRow
from tests.node_overlay_support import install_node_overlay

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
CORRELATION = UUID("8ea39bab-e7bb-4178-917e-749fbaf7aee4")
EFFECT = "node_work_progress_alert_effect"
COMPUTE = "node_work_progress_compute"
SETTINGS: dict[str, object] = {
    "ignore": {"lanes": ["ctl-*"], "actors": ["script:worker"]},
    "lookback_seconds": 86400,
    "check_interval_seconds": 300,
}
BUDGETS: dict[str, object] = {
    "budget_seconds": {"placed": 900, "claimed": 3600, "pr_open": 5400}
}


def row(
    age_seconds: int,
    row_type: str,
    lane: str,
    *,
    text: str = "",
    **cells: str,
) -> ModelLedgerRow:
    """A recorded row ``age_seconds`` before NOW; its id is the hash of its raw row."""
    row_ts = NOW - timedelta(seconds=age_seconds)
    body = " | ".join(f"{key}={value}" for key, value in cells.items())
    raw = f"{row_ts:%Y-%m-%dT%H:%M:%SZ} | {row_type} | lane={lane} | {body} | {text}"
    return ModelLedgerRow(
        row_id=hashlib.sha256(raw.strip().encode()).hexdigest(),
        row_ts=row_ts,
        row_type=row_type,
        row_lane=lane,
        raw_row=raw,
    )


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.value = at.timestamp()

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Reader:
    def __init__(self, rows: list[ModelLedgerRow], error: str | None = None) -> None:
        self.rows = rows
        self.error = error
        self.calls: list[datetime] = []

    def read(self, claimed_since: datetime) -> list[ModelLedgerRow]:
        self.calls.append(claimed_since)
        if self.error:
            raise RuntimeError(self.error)
        return list(self.rows)


class Publisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, topic: str, payload: bytes) -> None:
        self.calls.append((topic, json.loads(payload)))

    def on(self, suffix: str) -> list[dict[str, object]]:
        return [body for topic, body in self.calls if topic.endswith(suffix)]


def install_overlays(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    settings: dict[str, object] | None = None,
    budgets: dict[str, object] | None = None,
) -> None:
    install_node_overlay(monkeypatch, root, EFFECT, settings or SETTINGS)
    install_node_overlay(monkeypatch, root, COMPUTE, budgets or BUDGETS)


def resolve_channel(
    monkeypatch: pytest.MonkeyPatch, value: str = "C0TESTCHANNEL"
) -> None:
    monkeypatch.setattr(
        handler_work_progress_alert,
        "resolve_api_key_loop_safe",
        lambda _ref, **_kw: SecretStr(value),
    )


# A lane placed 100 minutes before NOW and silent since: over the 15 minute budget.
def stalled_lane(lane: str = "lane-a") -> list[ModelLedgerRow]:
    return [row(6000, "CLAIM", lane, host="h051", parent="orch-1")]
