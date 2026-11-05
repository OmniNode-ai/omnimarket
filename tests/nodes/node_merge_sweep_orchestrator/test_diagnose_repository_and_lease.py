# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the supplied one-lane plan: a diagnosis without a PR still needs a repo."""

from __future__ import annotations

import importlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_merge_sweep_effect.protocols import MergeSweepCommandOutcome
from omnimarket.nodes.node_merge_sweep_orchestrator.handlers import HandlerMergeSweepRun
from omnimarket.nodes.node_merge_sweep_orchestrator.handlers.handler_merge_sweep_stages_local import (
    LocalMergeSweepStages,
)
from tests.nodes.node_merge_sweep_orchestrator.support import run_request
from tests.nodes.node_merge_sweep_reading_compute.sweep_scenarios import load_recorded

# Only fields supplied by the incident brief; no invented PR, host or reason.
RECORDED_PLAN = {"kind": "diagnose", "repo": None, "prs": []}
RECORDED_RUN = "sweep-orchestrator-31d9-0152"


def _handler(node: str, operation: str) -> tuple[Any, Any]:
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{node}").joinpath("contract.yaml").read_text()
    )
    entry = next(
        e
        for e in contract["handler_routing"]["handlers"]
        if e["operation"] == operation
    )
    module, _, name = entry["input_model"].rpartition(".")
    handler = entry["handler"]
    return getattr(
        importlib.import_module(handler["module"]), handler["name"]
    )(), getattr(importlib.import_module(module), name)


def test_recorded_plan_builds_a_repository_even_without_a_pr() -> None:
    handler, request = _handler(
        "node_merge_sweep_plan_compute", "prepare_merge_sweep_dispatch"
    )
    plan = {
        "now": None,
        "lab_only": True,
        "load_per_core": None,
        "lanes": [RECORDED_PLAN],
        "dispatch": [RECORDED_PLAN],
        "skipped": [],
        "deferred": 0,
    }
    result = handler.handle(request(plan=plan, diagnose_repo="omnimarket"))
    assert result.dispatch[0].repo == "omnimarket"
    assert result.lanes[0].repo == "omnimarket"
    assert result.dispatch[0].prs == []


class Runner:
    def __init__(self, started: bool) -> None:
        self.started = started
        self.argvs: list[list[str]] = []

    def run(self, argv: Any, timeout_s: float) -> MergeSweepCommandOutcome:
        self.argvs.append(list(argv))
        if argv[2] == "run":
            # The real runner refuses this exact missing flag before starting.
            assert argv[argv.index("--repo") + 1] == "omnimarket"
            if not self.started:
                return MergeSweepCommandOutcome(2, "", "REMOTE-LANE-JOB-SPEC-REFUSED")
            return MergeSweepCommandOutcome(0, "receipt=/recorded.json", "")
        return MergeSweepCommandOutcome(0, "", "")


class LaneFiles:
    def write_text(self, path: str, text: str) -> None:
        pass

    def read_json(self, path: str) -> dict[str, Any]:
        # A started lane can fail its work; that still counts as a real start.
        return {"exit_code": 1, "status": "failed"}


@pytest.mark.parametrize("started", [False, True])
def test_dispatch_releases_only_when_no_lane_started(
    tmp_path: Path, started: bool
) -> None:
    recorded = load_recorded()
    request = run_request(
        recorded["scenarios"][0],
        recorded["base"],
        tmp_path,
        lane=RECORDED_RUN,
        max_lanes=1,
        retries=0,
        diagnose_repo="omnimarket",
        lease_path=str(tmp_path / "sweep.lease"),
    )
    runner = Runner(started)
    lane_handler, _ = _handler("node_merge_sweep_effect", "run_merge_sweep_lane")
    lane_handler._runner = runner
    lane_handler._files = LaneFiles()
    stages = LocalMergeSweepStages({"run_lane": lane_handler})
    result = HandlerMergeSweepRun(stages=stages).handle(request)
    assert result.outcomes[0].kind == "diagnose"
    assert result.outcomes[0].started is started
    lease = Path(request.lease_path)
    assert lease.exists() is started
    second = HandlerMergeSweepRun(stages=stages).handle(request)
    if started:
        assert not second.ok
        assert "LOCKED" in second.why
    else:
        assert len(runner.argvs) == 2
        assert not second.ok
        assert second.why == "all lanes failed to start"


def test_lease_release_cannot_remove_a_different_runs_lease(tmp_path: Path) -> None:
    handler, request = _handler("node_merge_sweep_effect", "manage_merge_sweep_lease")
    path = tmp_path / "sweep.lease"
    path.write_text(
        json.dumps({"run_id": "peer", "hold_until": "2099-01-01T00:00:00Z"})
    )
    result = handler.handle(
        request(
            path=str(path),
            run_id=RECORDED_RUN,
            action="release",
            now="2026-11-05T01:52:00Z",
        )
    )
    assert not result.ok
    assert json.loads(path.read_text())["run_id"] == "peer"


@pytest.mark.parametrize("field", ["diagnose_repo", "lease_path"])
def test_missing_deployment_settings_refuse_before_start_or_lease(
    tmp_path: Path, field: str
) -> None:
    recorded = load_recorded()
    request = run_request(recorded["scenarios"][0], recorded["base"], tmp_path)
    result = HandlerMergeSweepRun().handle(request.model_copy(update={field: ""}))
    assert not result.ok
    assert f"{field} is empty" in result.why
    assert not Path(request.lease_path).exists()


def test_a_concurrent_sweep_is_locked_while_the_first_is_starting(
    tmp_path: Path,
) -> None:
    recorded = load_recorded()
    request = run_request(
        recorded["scenarios"][0], recorded["base"], tmp_path, max_lanes=1, retries=0
    )
    observations = []

    class ConcurrentRunner(Runner):
        def run(self, argv: Any, timeout_s: float) -> MergeSweepCommandOutcome:
            observations.append(handler.handle(request))
            return super().run(argv, timeout_s)

    runner = ConcurrentRunner(False)
    lane_handler, _ = _handler("node_merge_sweep_effect", "run_merge_sweep_lane")
    lane_handler._runner = runner
    lane_handler._files = LaneFiles()
    handler = HandlerMergeSweepRun(
        stages=LocalMergeSweepStages({"run_lane": lane_handler})
    )
    result = handler.handle(request)
    assert result.why == "all lanes failed to start"
    assert len(observations) == 1
    assert "LOCKED" in observations[0].why
    assert len(runner.argvs) == 1
    assert not Path(request.lease_path).exists()


def test_wait_failure_keeps_the_hour_for_a_lane_that_already_started(
    tmp_path: Path,
) -> None:
    from datetime import datetime

    from omnimarket.nodes.node_merge_sweep_effect.protocols import MergeSweepPortError

    recorded = load_recorded()
    request = run_request(
        recorded["scenarios"][0], recorded["base"], tmp_path, max_lanes=1, retries=0
    )

    class WaitFailureRunner(Runner):
        def run(self, argv: Any, timeout_s: float) -> MergeSweepCommandOutcome:
            if argv[2] == "wait":
                raise MergeSweepPortError("wait unavailable")
            return super().run(argv, timeout_s)

    lane_handler, _ = _handler("node_merge_sweep_effect", "run_merge_sweep_lane")
    lane_handler._runner = WaitFailureRunner(True)
    lane_handler._files = LaneFiles()
    result = HandlerMergeSweepRun(
        stages=LocalMergeSweepStages({"run_lane": lane_handler})
    ).handle(request)
    assert result.ok
    assert result.outcomes[0].started
    assert result.outcomes[0].status == "wait-failed"
    lease = json.loads(Path(request.lease_path).read_text())
    assert (
        datetime.fromisoformat(lease["hold_until"])
        - datetime.fromisoformat(lease["completed_at"])
    ).total_seconds() == 3600


def test_a_started_attempt_is_preserved_when_the_other_host_cannot_start(
    tmp_path: Path,
) -> None:
    recorded = load_recorded()
    request = run_request(
        recorded["scenarios"][0], recorded["base"], tmp_path, max_lanes=1, retries=0
    )

    class RetryRunner(Runner):
        def run(self, argv: Any, timeout_s: float) -> MergeSweepCommandOutcome:
            if argv[2] == "run" and self.argvs:
                self.started = False
            return super().run(argv, timeout_s)

    class LimitedFiles(LaneFiles):
        def read_json(self, path: str) -> dict[str, Any]:
            return {"exit_code": 77, "status": "host-limited"}

    lane_handler, _ = _handler("node_merge_sweep_effect", "run_merge_sweep_lane")
    lane_handler._runner = RetryRunner(True)
    lane_handler._files = LimitedFiles()
    result = HandlerMergeSweepRun(
        stages=LocalMergeSweepStages({"run_lane": lane_handler})
    ).handle(request)
    assert result.ok
    assert result.outcomes[0].started
    assert result.outcomes[0].status == "start-failed"
    assert result.outcomes[0].events == ["start", "start-other-host"]
    assert Path(request.lease_path).exists()


def test_recorded_dispatch_over_the_canonical_runtime_releases_all_failed_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_merge_sweep_effect.protocols.local_merge_sweep_adapters import (
        LocalMergeSweepLaneLauncher,
    )
    from tests.nodes.node_merge_sweep_orchestrator.test_bus_cycle_merge_sweep_orchestrator import (
        _over_the_bus,
    )

    recorded = load_recorded()
    request = run_request(
        recorded["scenarios"][0],
        recorded["base"],
        tmp_path / "facts",
        lane=RECORDED_RUN,
        max_lanes=1,
        retries=0,
        brief_dir=str(tmp_path),
    )
    runner = Runner(False)

    def launch(self: Any, argv: Any, timeout_s: float) -> MergeSweepCommandOutcome:
        return runner.run(argv, timeout_s)

    monkeypatch.setattr(LocalMergeSweepLaneLauncher, "run", launch)
    _, result = _over_the_bus(request, tmp_path)
    assert not result.ok
    assert result.why == "all lanes failed to start"
    assert len(runner.argvs) == 1
    assert result.plan is not None
    assert result.plan.dispatch[0].repo == "omnimarket"
    assert not Path(request.lease_path).exists()
    brief = next(tmp_path.glob("*.brief.md")).read_text()
    assert "diagnose lane for omnimarket" in brief
