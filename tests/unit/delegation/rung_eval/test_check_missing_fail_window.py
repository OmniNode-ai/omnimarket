# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Only recent gaps fail the CLI; the report retains older gaps."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from omnimarket.delegation.rung_eval.cases import load_cases
from omnimarket.delegation.rung_eval.missing_nights import failing_gaps
from omnimarket.delegation.rung_eval.models import (
    EnumRungEvalStatus,
    ModelMissingNight,
    ModelRungEvalRow,
)
from omnimarket.delegation.rung_eval.rows_store import write_night
from omnimarket.delegation.rung_eval.rungs import load_rungs

pytestmark = pytest.mark.unit

THROUGH = date(2026, 10, 26)


def test_failing_gaps() -> None:
    gaps = [
        ModelMissingNight(
            night=THROUGH - timedelta(days=offset),
            rung_id="a",
            task_class="classification",
            case_id="case",
        )
        for offset in (0, 3)
    ]
    assert failing_gaps(gaps, THROUGH, 1) == gaps[:1]
    assert failing_gaps(gaps, THROUGH, 4) == gaps
    assert failing_gaps(gaps, THROUGH - timedelta(days=1), 2) == []
    with pytest.raises(ValueError, match="fail_window_days must be positive"):
        failing_gaps(gaps, THROUGH, 0)


@pytest.mark.parametrize(
    ("recent_unresolved", "fail_window_days", "exit_code"),
    [(False, None, 0), (True, None, 1), (False, 3, 1), (False, 0, 2)],
)
def test_check_missing_fail_window(
    tmp_path: Path,
    recent_unresolved: bool,
    fail_window_days: int | None,
    exit_code: int,
) -> None:
    rungs, cases = load_rungs(), load_cases()
    for night in (date(2026, 10, 24), THROUGH):
        rows = [
            ModelRungEvalRow(
                night=night,
                rung_id=rung.rung_id,
                task_class=case.task_class,
                case_id=case.case_id,
                status=(
                    EnumRungEvalStatus.UNRESOLVED
                    if rung == rungs[0] and (night != THROUGH or recent_unresolved)
                    else EnumRungEvalStatus.ERROR
                ),
                score=0.0,
                passed=False,
                detail="environment unset or call failed",
            )
            for rung in rungs
            for case in cases
        ]
        write_night(tmp_path, night, rows)
    root = Path(__file__).resolve().parents[4]
    command = [
        sys.executable,
        str(root / "scripts/ci/run_delegation_rung_eval.py"),
        "check-missing",
        "--dir",
        str(tmp_path),
        "--through",
        THROUGH.isoformat(),
        "--window-days",
        "3",
    ]
    if fail_window_days is not None:
        command.extend(["--fail-window-days", str(fail_window_days)])
    done = subprocess.run(
        command,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == exit_code, done.stderr
    if exit_code == 2:
        assert "--fail-window-days must be positive" in done.stderr
        assert done.stdout == ""
    else:
        lines = done.stdout.splitlines()
        assert any(line.startswith("MISSING 2026-10-24 |") for line in lines)
        assert any(line.startswith("MISSING 2026-10-25 |") for line in lines)
        assert any(line.startswith("MISSING 2026-10-26 |") for line in lines) == (
            recent_unresolved
        )
