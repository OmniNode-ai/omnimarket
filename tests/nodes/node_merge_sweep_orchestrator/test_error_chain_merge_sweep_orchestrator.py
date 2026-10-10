# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: a sweep that cannot read its facts starts no lane (OMN-20676)."""

from __future__ import annotations

import json
from pathlib import Path

from omnimarket.nodes.node_merge_sweep_orchestrator.handlers import (
    HandlerMergeSweepRun,
)

from ..node_merge_sweep_reading_compute.sweep_scenarios import load_recorded
from .support import RecordingStages, ScriptedRunner, run_request

RECORDED = load_recorded()


def test_a_stale_watcher_state_is_refused_and_no_lane_is_started(
    tmp_path: Path,
) -> None:
    spec = RECORDED["scenarios"][0]
    request = run_request(spec, RECORDED["base"], tmp_path, max_age_s=1.0)
    state = json.loads(Path(request.state_path).read_text())
    state["last_tick"] = "2020-01-01T00:00:00Z"
    Path(request.state_path).write_text(json.dumps(state))
    runner = ScriptedRunner({})
    result = HandlerMergeSweepRun(stages=RecordingStages(runner)).handle(request)
    assert result.ok is False
    assert result.why
    assert result.plan is None
    assert result.outcomes == []
    assert runner.argvs == []


def test_a_missing_state_file_is_refused_not_raised(tmp_path: Path) -> None:
    spec = RECORDED["scenarios"][0]
    request = run_request(spec, RECORDED["base"], tmp_path)
    Path(request.state_path).unlink()
    runner = ScriptedRunner({})
    result = HandlerMergeSweepRun(stages=RecordingStages(runner)).handle(request)
    assert result.ok is False
    assert runner.argvs == []
