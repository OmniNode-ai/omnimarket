# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The relation-map verb end to end through the CLI, on the file route with a stub append command."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_network_graph_compute.handlers.handler_import_relation_compute import (
    HandlerImportRelationCompute,
)
from omnimarket.nodes.node_reading_writer_effect.runtime_reading_writer import main
from tests.nodes.node_reading_writer_effect.test_reading_cli import _deployment
from tests.unit.nodes.node_network_graph_compute.relation_support import (
    edge,
    example_overlay,
    request,
)

pytestmark = pytest.mark.unit


def _report_file(tmp_path: Path) -> Path:
    report = HandlerImportRelationCompute().handle(
        request((edge("dispatch:f.py", "ledger-session:g.py"),), example_overlay())
    )
    path = tmp_path / "report.json"
    path.write_text(report.model_dump_json(), encoding="utf-8")
    return path


def test_relation_map_verb_appends_one_reading_row_for_the_piece(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = _deployment(tmp_path, monkeypatch)
    code = main(
        ["relation-map", "--piece", "1", "--report", str(_report_file(tmp_path))]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "RELATION-MAP piece=1 verdict=WORKING status=written" in out
    (row,) = sink.read_text(encoding="utf-8").splitlines()
    assert "piece=1" in row
    assert "name=relation_map" in row
    assert "reason=mapped=5/5,findings=4,cycles=0" in row


def test_relation_map_probe_appends_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = _deployment(tmp_path, monkeypatch)
    code = main(
        [
            "relation-map",
            "--piece",
            "1",
            "--report",
            str(_report_file(tmp_path)),
            "--probe",
        ]
    )
    assert code == 0
    assert "status=not-written" in capsys.readouterr().out
    assert not sink.exists()


def test_error_an_unreadable_report_is_indeterminate_and_writes_no_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = _deployment(tmp_path, monkeypatch)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code = main(["relation-map", "--piece", "1", "--report", str(bad)])
    assert code == 3
    assert "INDETERMINATE" in capsys.readouterr().err
    assert not sink.exists()
