# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stale or missing input ends the fill pass with a loud MISSING line first (OMN-26376).

The unit exited 69 on every pass and its err log held a REFUSED line some readers took for noise. The pass now writes
``MISSING <source> age=<seconds|absent|unknown> bound=<seconds>`` as the first line it writes on that refusal, naming the source, before the REFUSED
line the logs already carry. The pool snapshot's 180 s bound is the overlay's own value and is never read from here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import T0

from .test_runtime_lab_fill_tick import MAX_AGE_S, run, write_snapshot, write_unit

pytestmark = pytest.mark.unit

LEDGER_BOUND_S = 7200


def _first_line(err: str) -> str:
    return err.splitlines()[0]


def _ledger_unit(tmp_path: Path) -> dict[str, str]:
    return write_unit(
        tmp_path,
        work_sources={
            "integration_ledger": {
                "path": str(tmp_path / "integration-ledger.md"),
                "max_age_seconds": LEDGER_BOUND_S,
            },
            "pr_watcher": None,
            "goal_tickets": None,
        },
    )


def test_a_stale_pool_snapshot_is_the_first_line_and_names_age_and_bound(
    tmp_path: Path,
) -> None:
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    code, _, err, runner = run(env, tmp_path, age_s=MAX_AGE_S + 1)
    assert code == 69
    assert (
        _first_line(err)
        == f"MISSING pool-snapshot age={MAX_AGE_S + 1} bound={MAX_AGE_S}"
    )
    assert "REFUSED reason=pool-snapshot" in err
    assert runner.calls == []


def test_a_pool_snapshot_with_no_record_is_the_first_line_and_reads_absent(
    tmp_path: Path,
) -> None:
    env = write_unit(tmp_path)
    code, _, err, _ = run(env, tmp_path)
    assert code == 69
    assert _first_line(err) == f"MISSING pool-snapshot age=absent bound={MAX_AGE_S}"


def test_an_absent_integration_ledger_is_the_first_line_and_reads_absent(
    tmp_path: Path,
) -> None:
    env = _ledger_unit(tmp_path)
    write_snapshot(tmp_path)
    code, _, err, runner = run(env, tmp_path)
    assert code == 69
    assert (
        _first_line(err)
        == f"MISSING integration-ledger age=absent bound={LEDGER_BOUND_S}"
    )
    assert "REFUSED reason=work-source:integration-ledger" in err
    assert runner.calls == []


def test_a_stale_integration_ledger_is_the_first_line_and_names_age_and_bound(
    tmp_path: Path,
) -> None:
    env = _ledger_unit(tmp_path)
    write_snapshot(tmp_path)
    ledger = tmp_path / "integration-ledger.md"
    ledger.write_text("## NOT RUNNING\n", encoding="utf-8")
    stamp = T0.timestamp() - (LEDGER_BOUND_S + 100)
    os.utime(ledger, (stamp, stamp))
    code, _, err, _ = run(env, tmp_path)
    assert code == 69
    assert (
        _first_line(err)
        == f"MISSING integration-ledger age={LEDGER_BOUND_S + 100} bound={LEDGER_BOUND_S}"
    )


def test_a_stale_pr_watcher_is_the_first_line_and_names_its_bound(
    tmp_path: Path,
) -> None:
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    (tmp_path / "watcher-state.json").unlink()
    code, _, err, _ = run(env, tmp_path)
    assert code == 69
    assert _first_line(err) == "MISSING pr-watcher age=absent bound=300"


def test_a_pass_that_reads_every_input_writes_no_missing_line(tmp_path: Path) -> None:
    """Positive control: the line is the refusal's alone."""
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    code, _, err, _ = run(env, tmp_path)
    assert code == 0
    assert "MISSING" not in err
