# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The job-exit FRICTION row a tick writes agrees with what the same tick did about dispatch (OMN-22881).

The recorded case, ledger 2026-11-08T15:08:15Z and 15:25:02Z: the alarm wrote ``lane job-exit-fix dispatched for
episode ...`` and, in the STATUS row of the same tick, ``dispatched=no ... dispatch unconfigured``. These tests run
the real effect handler against a stand-in ``launchctl`` that reports one job whose last exit was 1, once with no
dispatch command (the host as recorded) and once with one (the control), and read the rows the tick wrote.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
    HandlerSilentFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelSilentFailureAlarmOptions,
    ModelSilentFailureAlarmRequest,
)
from tests.nodes.node_silent_failure_alarm_compute.test_golden_chain_silent_failure_alarm_compute import (
    HOST_NOW,
    world,
)

pytestmark = pytest.mark.unit

LABEL = "ai.omninode.work-ledger-copy"


def _launchctl(tmp_path: Path) -> Path:
    """A stand-in launchctl: ``list`` names one job, ``print`` reports 11 runs and a last exit code of 1."""
    script = tmp_path / "launchctl"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1] == 'list':\n"
        f"    print('-\\t1\\t{LABEL}')\n"
        "else:\n"
        "    print('\\truns = 11')\n"
        "    print('\\tlast exit code = 1')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _rows(tmp_path: Path, dispatch_command: tuple[str, ...] | None) -> tuple[str, ...]:
    options: ModelSilentFailureAlarmOptions = world(tmp_path).model_copy(
        update={
            "launchctl": str(_launchctl(tmp_path)),
            "dispatch_command": dispatch_command,
            "brief_dir": tmp_path / "briefs",
        }
    )
    result = HandlerSilentFailureAlarm(options).handle(
        ModelSilentFailureAlarmRequest(now=HOST_NOW, apply=True)
    )
    return tuple(row for row in result.rows if "class=job-exit" in row)


def test_a_host_with_no_dispatch_command_writes_a_row_that_sends_no_lane(
    tmp_path: Path,
) -> None:
    rows = _rows(tmp_path, None)
    assert len(rows) == 1, rows
    assert f"job={LABEL}" in rows[0]
    assert "dispatched" not in rows[0]
    assert "no lane sent for episode" in rows[0]


def test_a_host_with_a_dispatch_command_writes_a_row_that_dispatches_a_lane(
    tmp_path: Path,
) -> None:
    rows = _rows(tmp_path, (sys.executable, "-c", "pass"))
    assert len(rows) == 1, rows
    assert "lane job-exit-fix dispatched for episode" in rows[0]
    assert "no lane sent" not in rows[0]
