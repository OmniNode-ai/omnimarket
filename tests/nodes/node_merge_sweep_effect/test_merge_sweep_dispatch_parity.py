# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity: the nodes dispatch a sweep's lanes as the old workflow did (OMN-20676).

``fixtures/recorded_dispatch.json`` was recorded by running the real ``merge_sweep.js`` with its
``agent()`` calls scripted, over the lane plans of the recorded sweep (see
``node_merge_sweep_reading_compute``): each lane's runner exit codes were fed in order, and every
brief it wrote, every runner command it ran and every wait it took was recorded. The test drives the
same sweep through the nodes: the plan node caps the lanes and renders each brief, the effect node
starts each lane through a scripted runner, and the plan node decides each retry.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_merge_sweep_effect.handlers import (
    HandlerMergeSweepLaneRun,
)
from omnimarket.nodes.node_merge_sweep_effect.models import (
    ModelMergeSweepLaneRunRequest,
)
from omnimarket.nodes.node_merge_sweep_effect.protocols import MergeSweepCommandOutcome
from omnimarket.nodes.node_merge_sweep_plan_compute.handlers import (
    HandlerMergeSweepBrief,
    HandlerMergeSweepPlan,
    HandlerMergeSweepRetry,
    handler_merge_sweep_brief,
)
from omnimarket.nodes.node_merge_sweep_plan_compute.models import (
    ModelMergeSweepBriefRequest,
    ModelMergeSweepPlanRequest,
    ModelMergeSweepRetryRequest,
)

HERE = Path(__file__).parent
DISPATCH = json.loads((HERE / "fixtures" / "recorded_dispatch.json").read_text())
READING = json.loads(
    (
        HERE.parent
        / "node_merge_sweep_reading_compute"
        / "fixtures"
        / "recorded_sweep.json"
    ).read_text()
)
RULES_TOKEN = "\u0000RULES\u0000"
TICKET = "OMN-20107"
CASES = DISPATCH["cases"]


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
            base = lane.removesuffix("-r2")
            code = self.exits[base].pop(0)
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


def _sweep(case: dict[str, Any]) -> dict[str, Any]:
    """Run one recorded sweep through the nodes; return what the workflow recorded."""
    args = case["args"]
    reading = READING["scenarios"][case["scenario"]]["expected"]["reading"]
    max_lanes = int(args["max_lanes"]) if "max_lanes" in args else None
    retries = int(args["retries"]) if "retries" in args else None
    wait_min = int(args["retry_wait_min"]) if "retry_wait_min" in args else None
    planned = HandlerMergeSweepPlan().handle(
        ModelMergeSweepPlanRequest.model_validate(
            {"reading": reading, "max_lanes": max_lanes}
        )
    )
    runner = ScriptedRunner(case["exits"])
    files = ScriptedFiles(runner)
    lane_run = HandlerMergeSweepLaneRun(runner=runner, files=files)
    script = (
        f"{args['skill_dir'].rstrip('/')}/../remote-lane/scripts/onex_remote_lane.py"
    )
    events: dict[str, list[str]] = {}
    briefs: dict[str, list[str]] = {}
    receipts = []

    def start(lane: Any, name: str, base: str) -> Any:
        events.setdefault(base, []).append(
            "start-other-host" if name.endswith("-r2") else "start"
        )
        brief = HandlerMergeSweepBrief().handle(
            ModelMergeSweepBriefRequest(
                lane=name,
                sweep_lane=args["lane"],
                orchestrator=args["parent"],
                ticket=TICKET,
                kind=lane.kind,
                repo=lane.repo,
                prs=lane.prs,
                reasons=lane.reasons,
            )
        )
        briefs.setdefault(name, []).append(
            brief.text.replace(DISPATCH["rules"], RULES_TOKEN)
        )
        return lane_run.handle(
            ModelMergeSweepLaneRunRequest(
                runner_script=script,
                brief_path=f"/tmp/{base}.brief.md",
                brief_text=brief.text,
                lane=name,
                model=args["model"],
                parent=args["lane"],
                ticket=TICKET,
                prs=[p.pr for p in lane.prs],
                repo=lane.repo,
            )
        )

    for i, lane in enumerate(planned.dispatch):
        name = f"{args['lane']}-{lane.kind}-{i + 1}"
        receipt = start(lane, name, name)
        waits, other, retried = 0, False, None
        while True:
            decision = HandlerMergeSweepRetry().handle(
                ModelMergeSweepRetryRequest(
                    exit_code=receipt.exit_code,
                    waits_used=waits,
                    host_retry_used=other,
                    retries=retries,
                    retry_wait_min=wait_min,
                )
            )
            if decision.action == "wait_and_retry":
                events[name].append(f"wait:{decision.wait_min}")
                waits += 1
                receipt = start(lane, name, name)
            elif decision.action == "retry_other_host":
                other = True
                retried = {
                    "host": receipt.host,
                    "status": receipt.status,
                    "receipt": receipt.receipt,
                }
                receipt = start(lane, f"{name}-r2", name)
            else:
                break
        receipts.append(
            {
                "lane": name,
                "kind": lane.kind,
                "repo": lane.repo,
                "prs": [p.pr for p in lane.prs],
                "exit_code": receipt.exit_code,
                "status": receipt.status,
                "host": receipt.host,
                "retried_after": retried,
            }
        )
    commands: dict[str, list[str]] = {}
    for argv in runner.argvs:
        if argv[2] == "run":
            commands.setdefault(argv[argv.index("--lane") + 1], []).append(
                " ".join(argv)
            )
    return {
        "events": events,
        "commands": commands,
        "briefs": briefs,
        "receipts": receipts,
        "deferred": planned.deferred,
    }


def test_the_recorded_dispatch_covers_every_branch() -> None:
    assert len(CASES) >= 50
    seen = {e for c in CASES for v in c["expected"]["events"].values() for e in v}
    assert {"start", "start-other-host"} <= seen
    assert any(e.startswith("wait:") for e in seen)
    assert any(c["expected"]["deferred"] for c in CASES)
    assert {c["args"]["model"] for c in CASES} == {"sonnet", "opus"}
    assert any(r["retried_after"] for c in CASES for r in c["expected"]["receipts"])
    kinds = {r["kind"] for c in CASES for r in c["expected"]["receipts"]}
    assert kinds == {"diagnose", "escalation", "land-chain-head", "fix"}


@pytest.mark.parametrize("index", range(len(CASES)))
def test_dispatch_matches_the_workflow(index: int) -> None:
    case = CASES[index]
    got = _sweep(case)
    expected = case["expected"]
    assert got["events"] == expected["events"]
    assert got["commands"] == expected["commands"]
    assert got["briefs"] == expected["briefs"]
    assert got["receipts"] == expected["receipts"]
    assert got["deferred"] == expected["deferred"]


def test_a_broken_brief_fails_the_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: a brief that drops the no-merge rule disagrees with the workflow."""
    original = handler_merge_sweep_brief.STANDING_RULES
    monkeypatch.setattr(
        handler_merge_sweep_brief,
        "STANDING_RULES",
        original.replace("Premise", "Premise."),
    )
    misses = sum(_sweep(c)["briefs"] != c["expected"]["briefs"] for c in CASES)
    assert misses > 0
