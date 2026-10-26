# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the TLC result summary node."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_tlc_result_summary_effect.handlers import (
    handler_tlc_result_summary as mod,
)
from omnimarket.nodes.node_tlc_result_summary_effect.models import (
    ModelTlcResultSummaryRequest,
    ModelTlcResultSummaryResult,
)

pytestmark = pytest.mark.unit


def test_summarize_no_results(tmp_path: Path) -> None:
    rows = mod.summarize_run_directory(tmp_path)
    assert rows == []


def test_summarize_with_results(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    (results / "model1.out").write_text(
        "No error has been found\n"
        "1,234 distinct states found\n"
        "depth of the complete state graph search is 42\n"
        "State 1:\nState 2:\n"
    )
    (tmp_path / "model1.cfg").write_text("INVARIANTS prop1 prop2\n")
    rows = mod.summarize_run_directory(tmp_path)
    assert len(rows) == 1
    assert (
        rows[0]
        == "model1 | prop1 prop2 | No error has been found | 1,234 | depth 42 | trace 2"
    )


def test_main_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    results = tmp_path / "results"
    results.mkdir()
    (results / "m.out").write_text("Invariant Inv1 is violated\n")
    code = mod.main(["--run-dir", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "m |  | Invariant Inv1 is violated" in out


def test_handler_returns_typed_result(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    (results / "m.out").write_text("Invariant Inv1 is violated\n")
    result = mod.HandlerTlcResultSummary().handle(
        ModelTlcResultSummaryRequest(run_dir=tmp_path)
    )
    assert isinstance(result, ModelTlcResultSummaryResult)
    assert result.rows == tuple(mod.summarize_run_directory(tmp_path))
    assert len(result.rows) == 1
