# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the daily line effect (OMN-22925): one run publishes the line and writes one STATUS row."""

from __future__ import annotations

import ast
import json
import os
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_daily_line import (
    ModelDailyLinePublication,
    ModelDailyLineRunCommand,
)
from omnimarket.nodes.node_daily_line_compute.handlers.handler_daily_line import (
    HandlerDailyLine,
)
from omnimarket.nodes.node_daily_line_effect.handlers import handler_daily_line_run
from omnimarket.nodes.node_daily_line_effect.handlers.handler_daily_line_run import (
    HandlerDailyLineRun,
    load_runtime,
    write_atomic,
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
        compute=HandlerDailyLine(),
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


def test_error_chain_two_overlapping_runs_do_not_share_one_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second run that starts while the first is between write and replace must not take its temporary file (Tessl, OMN-22925)."""
    target = tmp_path / "out" / "daily-line.json"
    real_replace = os.replace
    entered: list[bool] = []

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if not entered:
            entered.append(True)
            write_atomic(target, "second\n")
        real_replace(src, dst)

    monkeypatch.setattr(handler_daily_line_run.os, "replace", replace)
    write_atomic(target, "first\n")
    assert target.read_text(encoding="utf-8") == "first\n"
    assert sorted(path.name for path in target.parent.iterdir()) == ["daily-line.json"]


def test_error_chain_a_floor_record_piece_with_no_reading_state_reads_as_unknown(
    tmp_path: Path,
) -> None:
    """A piece row with no reading_state is not a read piece 1: it is named in unknown_parts (Tessl, OMN-22925)."""
    home = tmp_path / "registry"
    directory = home / ".claude_scratch" / "factory-floor"
    directory.mkdir(parents=True)
    record = {
        "generated_at": "2026-11-08T10:28:00+00:00",
        "pieces": [{"piece": 1, "text": "no state here"}],
    }
    (directory / "floor-simple.json").write_text(json.dumps(record), encoding="utf-8")
    ledger = tmp_path / "ledger.md"
    ledger.write_text(LEDGER, encoding="utf-8")
    publication = HandlerDailyLineRun(
        load_runtime(),
        home=home,
        ledger=ledger,
        appender=[].append,
        clock=lambda: NOW,
        compute=HandlerDailyLine(),
    ).handle(ModelDailyLineRunCommand())
    assert "piece1" in publication.unknown_parts


def test_golden_chain_the_run_handler_imports_no_other_nodes_handler_package() -> None:
    source = Path(handler_daily_line_run.__file__).read_text(encoding="utf-8")
    imported = [
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("omnimarket.nodes.")
        and not node.module.startswith("omnimarket.nodes.node_daily_line_effect")
    ]
    assert imported == []
