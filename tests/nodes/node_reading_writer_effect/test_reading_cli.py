# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hourly verb end to end through the CLI, on the file route with a stub append command."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_reading_writer_effect.overlay import load_overlay
from omnimarket.nodes.node_reading_writer_effect.runtime_reading_writer import main
from tests.nodes.node_reading_writer_effect.reading_support import (
    START,
    healthy_ledger,
    overlay_document,
)

pytestmark = pytest.mark.unit

_APPENDER = """
import json, sys
rows = sys.stdin.read()
sink = sys.argv[sys.argv.index('--sink') + 1]
open(sink, 'a', encoding='utf-8').write(rows)
print(json.dumps({'status': 'accepted', 'message': 'ok'}))
"""


def _deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ledger = tmp_path / "state" / "LEDGER.md"
    ledger.parent.mkdir()
    ledger.write_text("\n".join(healthy_ledger()) + "\n", encoding="utf-8")
    script = tmp_path / "append.py"
    script.write_text(_APPENDER, encoding="utf-8")
    sink = tmp_path / "appended.md"
    document = overlay_document()
    document["ledger_append"]["command_env"] = "READING_TEST_APPEND"
    root = tmp_path / "overlays"
    (root / "node_reading_writer_effect").mkdir(parents=True)
    (root / "node_reading_writer_effect" / "overlay.yaml").write_text(
        yaml.safe_dump(document), encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    monkeypatch.setenv("READING_TEST_LEDGER", str(ledger))
    monkeypatch.setenv(
        "READING_TEST_APPEND", f"{sys.executable} {script} --sink {sink}"
    )
    monkeypatch.setenv("READING_TEST_HOST", "h-test")
    return sink


def test_hourly_writes_one_row_per_piece_for_a_window_through_the_configured_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = _deployment(tmp_path, monkeypatch)
    start = (START - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    code = main(["hourly", "--window-start", start])
    out = capsys.readouterr().out
    # a window a month old holds none of the fixture's rows: pieces 6 and 15 read NOT WORKING (stale),
    # which is still exit 0, because a probe refuses nothing
    assert code == 0, out
    assert out.count("READING piece=") == 3
    assert "verdict=NOT_WORKING" in out
    assert len(sink.read_text(encoding="utf-8").splitlines()) == 3


def test_probe_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = _deployment(tmp_path, monkeypatch)
    start = (START - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert main(["probe", "--window-start", start]) == 0
    assert "write=no" in capsys.readouterr().out
    assert not sink.exists()


def test_a_missing_deployment_fact_is_indeterminate_not_a_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _deployment(tmp_path, monkeypatch)
    monkeypatch.delenv("READING_TEST_LEDGER")
    assert main(["probe"]) == 3
    assert "READING_TEST_LEDGER" in capsys.readouterr().err


def test_a_window_still_open_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _deployment(tmp_path, monkeypatch)
    far_future = "2999-01-01T00:00:00Z"
    assert main(["probe", "--window-start", far_future]) == 1
    assert "window-open" in capsys.readouterr().err


def test_the_shipped_example_overlay_is_valid() -> None:
    example = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_reading_writer_effect/overlay.example.yaml"
    )
    overlay = load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": _example_root(example)})
    assert [piece.piece for piece in overlay.pieces] == list(overlay.order)
    assert all(piece.injected_fault for piece in overlay.pieces)


def _example_root(example: Path) -> str:
    # the loader looks for <root>/node_reading_writer_effect/overlay.yaml: the shipped directory
    # holds overlay.example.yaml, so stage a copy under that name
    import tempfile

    root = Path(tempfile.mkdtemp())
    (root / "node_reading_writer_effect").mkdir()
    (root / "node_reading_writer_effect" / "overlay.yaml").write_text(
        example.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return str(root)
