# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the diagnose lane's recorded missing-repository start refusal."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.merge_sweep.model_merge_sweep_plan import (
    ModelMergeSweepPlanRequest,
)
from omnimarket.nodes.node_merge_sweep_effect.protocols import MergeSweepCommandOutcome
from omnimarket.nodes.node_merge_sweep_orchestrator.handlers import (
    HandlerMergeSweepRun,
    LocalMergeSweepStages,
)

from ..node_merge_sweep_reading_compute.sweep_scenarios import load_recorded
from .support import RecordingStages, ScriptedRunner, run_request


class _RepoRequiredRunner(ScriptedRunner):
    def run(self, argv: Sequence[str], timeout_s: float) -> MergeSweepCommandOutcome:
        if argv[2] == "run" and "--repo" not in argv:
            self.argvs.append(list(argv))
            return MergeSweepCommandOutcome(
                2, "", "REMOTE-LANE-JOB-SPEC-REFUSED missing --repo"
            )
        return super().run(argv, timeout_s)


def test_recorded_diagnose_start_refusal_is_not_repeated(tmp_path: Path) -> None:
    """Read, plan, brief and launch through the contract-routed production stages."""
    recorded = load_recorded()
    lane = "sweep-orchestrator-31d9-1536"
    name = f"{lane}-diagnose-1"
    runner = _RepoRequiredRunner({name: [0]})
    result = HandlerMergeSweepRun(stages=RecordingStages(runner)).handle(
        run_request(
            recorded["scenarios"][0],
            recorded["base"],
            tmp_path,
            lane=lane,
            ticket="OMN-19998",
            max_lanes=1,
        )
    )
    assert result.ok, result.why
    (outcome,) = result.outcomes
    assert outcome.kind == "diagnose"
    assert outcome.status == "done"
    assert outcome.exit_code == 0
    starts = [argv for argv in runner.argvs if argv[2] == "run"]
    assert len(starts) == 1
    assert starts[0][starts[0].index("--repo") + 1] == "omnimarket"
    assert outcome.repo == "omnimarket"


def test_refusal_replay_rejects_a_start_without_a_repo() -> None:
    runner = _RepoRequiredRunner({})
    refused = runner.run(["python3", "runner.py", "run"], 1)
    assert refused.returncode == 2
    assert refused.stderr == "REMOTE-LANE-JOB-SPEC-REFUSED missing --repo"


@pytest.mark.parametrize(
    "reading",
    [
        {},
        {"controller": {}, "product": {}},
        {"controller": {"stalled": True}, "product": {"under_floor": []}},
        {
            "controller": {"stalled": False},
            "product": {"under_floor": ["omnimarket", "omnibase_core"]},
        },
        {
            "controller": {"stalled": False},
            "product": {"under_floor": ["unread"]},
        },
    ],
)
def test_every_diagnose_trigger_names_the_workflow_repository(
    reading: dict[str, Any],
) -> None:
    plan = LocalMergeSweepStages().plan(
        ModelMergeSweepPlanRequest.model_validate({"reading": reading})
    )
    (lane,) = plan.dispatch
    assert lane.kind == "diagnose"
    assert lane.repo == "omnimarket"
