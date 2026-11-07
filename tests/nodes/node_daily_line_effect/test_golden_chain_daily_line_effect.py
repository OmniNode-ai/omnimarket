# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the daily line effect (OMN-22925): one run publishes the line and writes one STATUS row."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_daily_line import (
    ModelDailyLinePublication,
    ModelDailyLineRunCommand,
)
from omnimarket.nodes.node_daily_line_effect.handlers.handler_daily_line_run import (
    HandlerDailyLineRun,
    load_runtime,
)

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.daily-line-published.v1"
NOW = datetime(2026, 11, 8, 10, 30, tzinfo=UTC)
FLOOR = {
    "generated_at": "2026-11-08T10:28:00+00:00",
    "pieces": [
        {
            "piece": 1,
            "reading_state": "WORKING",
            "text": "2 nightly runs in 24 hours",
            "joins": [{"id": "J1", "state": "passed"}],
        }
    ],
}
LEDGER = "2026-11-08T08:00:00Z | CLAIM | lane=a | ticket=OMN-1 | actor=x | model=y | est ~1 lane-hours; displaces z; (OMN-1)\n"


def _run(
    tmp_path: Path, *, floor: bool, ledger: bool
) -> tuple[ModelDailyLinePublication, list[str], Path]:
    home = tmp_path / "registry"
    directory = home / ".claude_scratch" / "factory-floor"
    directory.mkdir(parents=True)
    if floor:
        (directory / "floor-simple.json").write_text(
            json.dumps(FLOOR), encoding="utf-8"
        )
    ledger_path = tmp_path / "ledger.md"
    if ledger:
        ledger_path.write_text(LEDGER, encoding="utf-8")
    rows: list[str] = []
    handler = HandlerDailyLineRun(
        load_runtime(),
        home=home,
        ledger=ledger_path,
        appender=rows.append,
        clock=lambda: NOW,
    )
    return handler.handle(ModelDailyLineRunCommand()), rows, handler.published_path


def test_golden_chain_daily_line_effect_declares_its_runtime_and_schedule() -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_daily_line_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    assert contract["name"] == "node_daily_line_effect"
    assert contract["node_type"] == "effect"
    assert contract["terminal_event"] == TERMINAL
    assert contract["externally_consumed_topics"] == [TERMINAL]
    assert contract["schedule"]["cadence"]["time_zone"] == "America/New_York"
    assert contract["schedule"]["cadence"]["on_calendar"] == "*-*-* 05:30:00"
    assert contract["runtime"]["published_relpath"].endswith("daily-line.json")


def test_golden_chain_daily_line_effect_publishes_and_writes_one_status_row(
    tmp_path: Path,
) -> None:
    publication, rows, published = _run(tmp_path, floor=True, ledger=True)
    assert publication.unknown_parts == ()
    assert publication.line.endswith("by J1: passed; claims with no TERMINAL: 1.")
    assert json.loads(published.read_text(encoding="utf-8"))["line"] == publication.line
    assert len(rows) == 1
    assert " | STATUS | lane=node_daily_line_effect | ticket=OMN-22925 | " in rows[0]


def test_error_chain_daily_line_effect_with_no_source_still_publishes_and_writes_one_row(
    tmp_path: Path,
) -> None:
    publication, rows, published = _run(tmp_path, floor=False, ledger=False)
    assert publication.unknown_parts == ("piece1", "j1", "claims")
    assert published.is_file()
    assert len(rows) == 1
    assert "unknown=piece1,j1,claims | " in rows[0]
