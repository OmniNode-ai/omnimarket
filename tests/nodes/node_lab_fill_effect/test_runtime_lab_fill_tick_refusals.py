# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Mac lab-fill unit's entry: the error chain (OMN-24419).

An input the pass cannot read ends it with a typed REFUSED line and a non-zero exit before the tick runs: a pool
snapshot past its bound or with no record, a ledger read that is not ok, an unreadable approved-work list, an overlay
that does not load. Each case asserts that no lane was launched and the heartbeat records the exit. The failing
control is the stale snapshot: an entry that read the pool without its bound would launch lanes into a stale count.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .test_lab_fill_select import FakeLedgerRows
from .test_runtime_lab_fill_tick import MAX_AGE_S, run, write_snapshot, write_unit

pytestmark = pytest.mark.unit


def test_a_snapshot_past_its_bound_launches_nothing(tmp_path: Path) -> None:
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    code, out, err, runner = run(env, tmp_path, age_s=MAX_AGE_S + 1)
    assert code == 69
    assert out == ""
    assert "REFUSED reason=pool-snapshot" in err
    assert "stale" in err
    assert runner.calls == []
    heartbeat = json.loads((tmp_path / "state" / "last-run.json").read_text("utf-8"))
    assert heartbeat["exit_code"] == 69


def test_a_snapshot_at_its_bound_is_still_read(tmp_path: Path) -> None:
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    code, _, _, runner = run(env, tmp_path, age_s=MAX_AGE_S)
    assert code == 0
    assert len(runner.calls) == 2


def test_a_missing_snapshot_launches_nothing(tmp_path: Path) -> None:
    env = write_unit(tmp_path)
    code, out, err, runner = run(env, tmp_path)
    assert code == 69
    assert out == ""
    assert "REFUSED reason=pool-snapshot" in err
    assert runner.calls == []


@pytest.mark.parametrize("status", ["stale", "unreadable", "not-moved"])
def test_a_ledger_read_that_is_not_ok_launches_nothing(
    tmp_path: Path, status: str
) -> None:
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    code, out, err, runner = run(
        env, tmp_path, ledger=FakeLedgerRows(status=status, reason="why")
    )
    assert code == 69
    assert out == ""
    assert f"REFUSED reason=ledger-{status} detail=why" in err
    assert runner.calls == []


def test_an_unreadable_approved_work_list_launches_nothing(tmp_path: Path) -> None:
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    (tmp_path / "approved-work.json").write_text("{not json", encoding="utf-8")
    code, out, err, runner = run(env, tmp_path)
    assert code == 69
    assert out == ""
    assert "REFUSED reason=approved-work" in err
    assert runner.calls == []


def test_an_overlay_that_does_not_load_is_exit_78(tmp_path: Path) -> None:
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    overlay_file = tmp_path / "overlays" / "node_lab_fill_effect" / "overlay.yaml"
    overlay_file.write_text("parent: orchestrator-bb1a\n", encoding="utf-8")
    code, out, err, runner = run(env, tmp_path)
    assert code == 78
    assert out == ""
    assert "REFUSED reason=overlay" in err
    assert runner.calls == []


def test_no_overlay_root_is_exit_78(tmp_path: Path) -> None:
    write_snapshot(tmp_path)
    code, _, err, runner = run({}, tmp_path)
    assert code == 78
    assert "REFUSED reason=overlay" in err
    assert runner.calls == []
