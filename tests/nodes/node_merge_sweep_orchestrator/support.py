# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scripted runner, files and request builder for the merge-sweep orchestrator tests (OMN-20676)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_merge_sweep_effect.handlers import HandlerMergeSweepLaneRun
from omnimarket.nodes.node_merge_sweep_effect.protocols import MergeSweepCommandOutcome
from omnimarket.nodes.node_merge_sweep_orchestrator.handlers import (
    LocalMergeSweepStages,
)
from omnimarket.nodes.node_merge_sweep_orchestrator.models import (
    ModelMergeSweepRunRequest,
    ModelMergeSweepSupplement,
)

from ..node_merge_sweep_reading_compute.sweep_scenarios import write_files


class ScriptedRunner:
    """The remote-lane runner as the workflow's scripted agents saw it."""

    def __init__(self, exits: dict[str, list[int]]) -> None:
        self.exits = {name: list(codes) for name, codes in exits.items()}
        self.argvs: list[list[str]] = []
        self.receipts: dict[str, dict[str, Any]] = {}
        self._waits: dict[str, int] = {}

    def run(self, argv: Sequence[str], timeout_s: float) -> MergeSweepCommandOutcome:
        self.argvs.append(list(argv))
        if argv[2] == "run":
            lane = argv[argv.index("--lane") + 1]
            code = self.exits[lane.removesuffix("-r2")].pop(0)
            path = f"/tmp/{lane}.json"
            self.receipts[path] = {
                "exit_code": code,
                "status": "done" if code == 0 else "failed",
                "host": f"h{code}",
                "duration_s": 1,
                "result": "r",
            }
            return MergeSweepCommandOutcome(
                0, f"DETACHED lane={lane} pid=7 receipt={path} out=/x\n", ""
            )
        path = argv[argv.index("--receipt") + 1]
        self._waits[path] = self._waits.get(path, 0) + 1
        return MergeSweepCommandOutcome(3 if self._waits[path] == 1 else 0, "", "")


class ScriptedFiles:
    def __init__(self, runner: ScriptedRunner) -> None:
        self._runner = runner
        self.written: list[tuple[str, str]] = []

    def write_text(self, path: str, text: str) -> None:
        self.written.append((path, text))

    def read_json(self, path: str) -> dict[str, Any] | None:
        return self._runner.receipts.get(path)


class RecordingStages(LocalMergeSweepStages):
    """The local stages with the runner scripted; remembers every brief and every wait."""

    def __init__(self, runner: ScriptedRunner) -> None:
        super().__init__(
            {
                "run_lane": HandlerMergeSweepLaneRun(
                    runner=runner, files=ScriptedFiles(runner)
                )
            }
        )
        self.briefs: dict[str, list[str]] = {}

    def brief(self, request: Any) -> Any:
        out = super().brief(request)
        self.briefs.setdefault(request.lane, []).append(out.text)
        return out


def run_request(
    spec: dict[str, Any],
    base: dict[str, Any],
    root: Path,
    **overrides: Any,
) -> ModelMergeSweepRunRequest:
    """The request that runs a recorded scenario's files; the scenario supplies what the watcher lacks."""
    paths = write_files(base, spec, root)
    fields: dict[str, Any] = {
        "state_path": str(paths["state"]),
        "ledger_path": str(paths["ledger"]),
        "ticks_path": str(paths["ticks"]) if "ticks" in paths else None,
        "floors_path": str(paths["floors"]),
        "now": spec["now"],
        "load1": spec["load1"],
        "cpus": spec["cpus"],
        "window_min": spec["window_min"],
        "max_reds": spec["max_reds"],
        "supplement": ModelMergeSweepSupplement(
            files=spec.get("files", {}),
            ready_at=spec.get("ready_at", {}),
            merge_files=spec.get("merge_files", {}),
        ),
        "lane": "sweep-0",
        "parent": "orch-8",
        "ticket": "OMN-20107",
        "model": "sonnet",
        "runner_script": "/x/skills/remote-lane/scripts/onex_remote_lane.py",
        "brief_dir": "/tmp",
    }
    fields.update(overrides)
    return ModelMergeSweepRunRequest(**fields)
