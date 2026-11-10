# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The CLI of the defect scan (OMN-23331): exit 0 for a read row (red or not), 1 for a failed run, 3 for a missing deployment fact."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnimarket.nodes.node_delegation_events_defect_scan_effect import (
    runtime_delegation_events_defect_scan as cli,
)
from tests.nodes.node_delegation_events_defect_scan_compute.defect_scan_support import (
    OVERLAY_FILE as COMPUTE_OVERLAY,
)
from tests.nodes.node_delegation_events_defect_scan_compute.defect_scan_support import (
    planted,
)
from tests.nodes.node_delegation_events_defect_scan_effect.defect_scan_run_support import (
    OVERLAY_FILE as EFFECT_OVERLAY,
)
from tests.nodes.node_delegation_events_defect_scan_effect.defect_scan_run_support import (
    handler,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def overlay_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    roots = [
        str(EFFECT_OVERLAY.parents[1]),
        str(COMPUTE_OVERLAY.parents[1]),
    ]
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", os.pathsep.join(roots))


def test_delegation_events_defect_scan_cli_without_an_overlay_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    assert cli.main(["probe"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


@pytest.mark.usefixtures("overlay_roots")
def test_delegation_events_defect_scan_cli_probe_of_a_defective_day_prints_the_reading_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run, _ = handler(events=planted("unattributed", 4), write=False)
    monkeypatch.setattr(cli, "build_handler", lambda *_a: run)
    assert cli.main(["probe"]) == 0
    out = capsys.readouterr().out
    assert "verdict=NOT_WORKING" in out
    assert "unattributed=4" in out
    assert "ledger=not-written" in out


@pytest.mark.usefixtures("overlay_roots")
def test_delegation_events_defect_scan_cli_scan_with_no_appender_fails_with_exit_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run, _ = handler(write=False)
    monkeypatch.setattr(cli, "build_handler", lambda *_a: run)
    assert cli.main(["scan"]) == 1
    assert "DEFECT-SCAN-FAILED no-appender-for-a-writing-run" in capsys.readouterr().err


def test_delegation_events_defect_scan_cli_schedule_needs_an_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        cli.main(["schedule"])


def test_delegation_events_defect_scan_cli_schedule_uninstall_runs_the_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        cli.schedule_module,
        "uninstall",
        lambda _schedule, *, home: calls.append(str(home)) or 0,
    )
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda _c: tmp_path))
    assert cli.main(["schedule", "uninstall"]) == 0
    assert calls == [str(tmp_path)]
