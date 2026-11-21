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


def test_a_missing_approved_work_file_names_its_path(tmp_path: Path) -> None:
    """The unit's log held 73 lines reading only FileNotFoundError: the line must say which file was absent."""
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    missing = tmp_path / "approved-work.json"
    missing.unlink()
    code, out, err, runner = run(env, tmp_path)
    assert code == 69
    assert out == ""
    assert "REFUSED reason=approved-work detail=FileNotFoundError" in err
    assert f"path={missing}" in err
    assert runner.calls == []


def test_a_missing_selection_overlay_names_its_id_and_the_roots_searched(
    tmp_path: Path,
) -> None:
    """The select step's overlay is a second overlay: the refusal line must tell it from the unit's own."""
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    (
        tmp_path / "overlays" / "node_lab_fill_selection_compute" / "overlay.yaml"
    ).unlink()
    code, out, err, runner = run(env, tmp_path)
    assert code == 78
    assert out == ""
    assert "REFUSED reason=overlay detail=NodeOverlayNotFoundError" in err
    assert "node_lab_fill_selection_compute" in err
    assert str(tmp_path / "overlays") in err
    assert runner.calls == []


def test_a_missing_unit_overlay_names_its_id_and_the_roots_searched(
    tmp_path: Path,
) -> None:
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    (tmp_path / "overlays" / "node_lab_fill_effect" / "overlay.yaml").unlink()
    code, _, err, runner = run(env, tmp_path)
    assert code == 78
    assert "REFUSED reason=overlay detail=NodeOverlayNotFoundError" in err
    assert "node_lab_fill_effect" in err
    assert str(tmp_path / "overlays") in err
    assert runner.calls == []


def test_an_invalid_overlay_names_only_its_error_type(tmp_path: Path) -> None:
    """The control: an overlay that loads wrong may hold private values, so only the type is written."""
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    overlay_file = tmp_path / "overlays" / "node_lab_fill_effect" / "overlay.yaml"
    overlay_file.write_text("parent: SECRET-VALUE-9\n", encoding="utf-8")
    code, _, err, _ = run(env, tmp_path)
    assert code == 78
    assert "SECRET-VALUE-9" not in err
    assert "REFUSED reason=overlay detail=NodeOverlayError\n" in err


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
