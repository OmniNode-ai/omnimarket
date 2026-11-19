# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves to its handler, and a reading runs through to one alarm and one recovery row."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_temporal_health_alarm_effect.handlers.temporal_cli_reader import (
    read_facts,
)
from omnimarket.nodes.node_temporal_health_alarm_effect.models.model_temporal_health_alarm import (
    ModelTemporalHealthAlarmRequest,
)
from omnimarket.nodes.node_temporal_health_alarm_effect.runtime_temporal_health_alarm import (
    load_settings,
)

pytestmark = pytest.mark.unit

NAME = "node_temporal_health_alarm_effect"
NOW = datetime(2026, 11, 16, 16, 0, 0, tzinfo=UTC)


def test_golden_chain_temporal_health_alarm_effect(tmp_path: Path) -> None:
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    assert contract["name"] == NAME
    assert contract["schedule"]["start_interval_s"] == 60
    assert contract["config"]["poll_stale_after_s"] == 600
    (entry,) = contract["handler_routing"]["handlers"]
    handler_cls = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    settings = load_settings({})
    rows: list[str] = []

    def append(row: str) -> str:
        rows.append(row)
        return ""

    handler = handler_cls(settings=settings, append=append)
    fresh = json.dumps({"pollers": [{"lastAccessTime": "2026-11-16T16:01:55Z"}]})

    def down_runner(argv: list[str], timeout_s: float) -> tuple[int, str, str]:
        return (0, "SERVING", "") if "health" in argv else (0, "{}", "")

    def up_runner(argv: list[str], timeout_s: float) -> tuple[int, str, str]:
        return (0, "SERVING", "") if "health" in argv else (0, fresh, "")

    for minute in range(3):
        facts = read_facts(
            settings, now=NOW + timedelta(seconds=30 * minute), runner=down_runner
        )
        handler.handle(
            ModelTemporalHealthAlarmRequest(
                facts=facts, host="h051", state_dir=tmp_path
            )
        )
    facts = read_facts(settings, now=NOW + timedelta(minutes=2), runner=up_runner)
    result = handler.handle(
        ModelTemporalHealthAlarmRequest(facts=facts, host="h051", state_dir=tmp_path)
    )

    assert len(rows) == 2
    assert "| state=alarm |" in rows[0]
    assert "reasons=worker-not-polling,no-task-polled" in rows[0]
    assert "| state=recovered |" in rows[1]
    assert result.row_kind == "recovery"
    assert result.outcome == "appended"
