# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared rung list holds no cloud rung (OMN-24155).

The nightly eval's declared list no longer names the cloud-openrouter rung. It
declares exactly the three lab/planner rungs, none of which pins a backend or
tenant, so the check-missing gate is green for the remaining rungs and goes red
for exactly a rung whose environment is unset.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from omnimarket.delegation.rung_eval.cases import load_cases
from omnimarket.delegation.rung_eval.missing_nights import (
    detect_missing_nights,
    failing_gaps,
)
from omnimarket.delegation.rung_eval.models import ModelRungSpec, ModelTransportResult
from omnimarket.delegation.rung_eval.rows_store import read_rows, write_night
from omnimarket.delegation.rung_eval.rungs import load_rungs
from omnimarket.delegation.rung_eval.runner import RungUnresolvedError, run_night

pytestmark = pytest.mark.unit

_NIGHTS = (date(2026, 10, 25), date(2026, 10, 26))


class _Transport:
    """Answers every rung it is asked; raises for a rung in ``unresolved``."""

    def __init__(self, unresolved: set[str] | None = None) -> None:
        self.unresolved = unresolved or set()

    def complete(self, rung: ModelRungSpec, prompt: str) -> ModelTransportResult:
        if rung.rung_id in self.unresolved:
            raise RungUnresolvedError(f"{rung.rung_id}: environment variable unset")
        return ModelTransportResult(text="an answer", model_id="m", latency_ms=1)


def _write_nights(
    tmp_path: Path, rungs: list[ModelRungSpec], transport: _Transport
) -> None:
    cases = load_cases()
    for night in _NIGHTS:
        write_night(tmp_path, night, run_night(night, rungs, cases, transport))


def test_the_declared_list_holds_no_cloud_rung() -> None:
    rungs = load_rungs()
    assert len(rungs) == 3
    assert {r.kind for r in rungs} <= {"local", "planner"}
    assert all(r.kind != "cloud" for r in rungs)
    assert all(r.backend_id_env is None and r.tenant_env is None for r in rungs)
    root = Path(__file__).resolve().parents[4]
    text = (root / "src/omnimarket/configs/delegation_rung_eval.v1.yaml").read_text()
    assert not any(
        "OPENROUTER" in line
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_check_runs_green_with_the_remaining_rungs(tmp_path: Path) -> None:
    _write_nights(tmp_path, load_rungs(), _Transport())
    root = Path(__file__).resolve().parents[4]
    done = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/ci/run_delegation_rung_eval.py"),
            "check-missing",
            "--dir",
            str(tmp_path),
            "--through",
            "2026-10-26",
            "--window-days",
            "2",
        ],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""


def test_failing_control_a_rung_with_unset_environment_fails_the_check(
    tmp_path: Path,
) -> None:
    rungs = load_rungs()
    planted = ModelRungSpec(
        rung_id="cloud-planted",
        kind="cloud",
        backend_id_env="PLANTED_BACKEND",
        tenant_env="PLANTED_TENANT",
    )
    rungs_with_planted = [*rungs, planted]
    _write_nights(
        tmp_path, rungs_with_planted, _Transport(unresolved={"cloud-planted"})
    )
    cases = load_cases()
    gaps = detect_missing_nights(
        read_rows(tmp_path),
        rungs_with_planted,
        cases,
        through=date(2026, 10, 26),
        window_days=2,
    )
    failing = failing_gaps(gaps, date(2026, 10, 26), 1)
    assert failing
    assert all(g.rung_id == "cloud-planted" for g in failing)
    assert all(g.reason.startswith("unresolved:") for g in failing)
