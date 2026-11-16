# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick form of the ruling register: the unit's run reads the register and records the reading (OMN-24944).

Golden: a probe reads nothing and says ``PROBE ok``; a run prints one MISSING line per row that is not TRUE, one
``RULING-REGISTER`` summary line, exits 0 (a row that is not TRUE is the reading, not a failed tick) and writes
``last-run.json`` with the verdict counts. Error: with no overlay a probe and a run exit 3 and a run writes no
evidence. Failing control: a run with every row TRUE records zero FALSE and zero UNREAD, so the counts are read from
the verdicts and are not constants.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_ruling_register_effect.runtime_ruling_register_tick import (
    main,
)
from tests.unit.nodes.node_ruling_register_effect.test_ruling_register_effect import (
    cli_env,
    cli_row,
)

pytestmark = pytest.mark.unit


def _record(state_dir: Path) -> dict[str, object]:
    return json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))


def test_the_probe_says_ok_and_runs_no_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "ran"
    cli_env(
        tmp_path,
        monkeypatch,
        [
            cli_row(
                "row-marker",
                f"open({str(marker)!r}, 'w').close()",
                {"kind": "exit_zero"},
            )
        ],
    )
    assert main(["--probe"]) == 0
    assert capsys.readouterr().out == "PROBE ok\n"
    assert not marker.exists()


def test_the_probe_and_the_run_exit_3_with_no_overlay_and_the_run_writes_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    assert main(["--probe"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
    state_dir = tmp_path / "state"
    assert main(["--omni-home", str(tmp_path), "--state-dir", str(state_dir)]) == 3
    assert "ONEX_SKILL_OVERLAY_ROOTS" in capsys.readouterr().err
    assert not (state_dir / "last-run.json").exists()


def test_a_run_prints_the_missing_lines_and_records_the_verdict_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env(
        tmp_path,
        monkeypatch,
        [
            cli_row("row-true", "pass", {"kind": "exit_zero"}),
            cli_row("row-false", "print('leftover')", {"kind": "stdout_empty"}),
            cli_row("row-dies", "import sys; sys.exit(9)", {"kind": "exit_zero"}),
        ],
    )
    state_dir = tmp_path / "state"
    assert main(["--omni-home", str(tmp_path), "--state-dir", str(state_dir)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split(" | ")[0] for line in lines[:-1]] == [
        "MISSING ruling-not-running:row-false",
        "MISSING ruling-not-running:row-dies",
    ]
    assert lines[-1] == "RULING-REGISTER rows=3 TRUE=1 FALSE=1 UNREAD=1"
    record = _record(state_dir)
    assert record["passed"] is False
    assert record["rows"] == 3
    assert record["counts"] == {"TRUE": 1, "FALSE": 1, "UNREAD": 1}
    assert record["verdicts"] == {
        "row-true": "TRUE",
        "row-false": "FALSE",
        "row-dies": "UNREAD",
    }


def test_failing_control_a_run_with_every_row_true_records_no_false_and_no_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env(
        tmp_path,
        monkeypatch,
        [cli_row("row-true", "pass", {"kind": "exit_zero"})],
    )
    state_dir = tmp_path / "state"
    assert main(["--state-dir", str(state_dir)]) == 0
    assert capsys.readouterr().out == "RULING-REGISTER rows=1 TRUE=1 FALSE=0 UNREAD=0\n"
    record = _record(state_dir)
    assert record["passed"] is True
    assert record["counts"] == {"TRUE": 1, "FALSE": 0, "UNREAD": 0}


def test_a_run_with_no_state_dir_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_env(tmp_path, monkeypatch, [cli_row("row-true", "pass", {"kind": "exit_zero"})])
    assert main([]) == 0
    assert not list(tmp_path.glob("**/last-run.json"))
