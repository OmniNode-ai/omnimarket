# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity: one request to the orchestrator runs a whole sweep as the old workflow did (OMN-20676).

The recorded dispatch (``node_merge_sweep_effect/fixtures/recorded_dispatch.json``) was recorded by
running the real ``merge_sweep.js`` with its ``agent()`` calls scripted, over the lane plans of the
recorded sweep (``node_merge_sweep_reading_compute/fixtures/recorded_sweep.json``). This test hands
the orchestrator the state, ledger, tick-log and floors files of the same scenario and the same
scripted runner exit codes, and compares everything the workflow recorded: the starts and waits of
every lane, the runner commands, every brief, every lane receipt and the deferred count. The reading
and the plan of the scenario are the old tools' own and are compared too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_merge_sweep_orchestrator.handlers import (
    HandlerMergeSweepRun,
)
from omnimarket.nodes.node_merge_sweep_plan_compute.handlers import (
    handler_merge_sweep_brief,
)

from ..node_merge_sweep_reading_compute.sweep_scenarios import load_recorded
from .support import RecordingStages, ScriptedRunner, run_request

HERE = Path(__file__).parent
DISPATCH = json.loads(
    (
        HERE.parent / "node_merge_sweep_effect" / "fixtures" / "recorded_dispatch.json"
    ).read_text()
)
RECORDED = load_recorded()
RULES_TOKEN = "\u0000RULES\u0000"
CASES = DISPATCH["cases"]


def _sweep(case: dict[str, Any], root: Path) -> tuple[dict[str, Any], Any, list[float]]:
    """Run one recorded sweep through the orchestrator; return what the workflow recorded."""
    args = case["args"]
    spec = RECORDED["scenarios"][case["scenario"]]
    runner = ScriptedRunner(case["exits"])
    stages = RecordingStages(runner)
    slept: list[float] = []
    overrides: dict[str, Any] = {
        "lane": args["lane"],
        "parent": args["parent"],
        "model": args["model"],
        "runner_script": f"{args['skill_dir'].rstrip('/')}/../remote-lane/scripts/onex_remote_lane.py",
    }
    for key in ("max_lanes", "retries", "retry_wait_min"):
        if key in args:
            overrides[key] = int(args[key])
    result = HandlerMergeSweepRun(stages=stages, sleep=slept.append).handle(
        run_request(spec, RECORDED["base"], root, **overrides)
    )
    assert result.ok, result.why
    commands: dict[str, list[str]] = {}
    for argv in runner.argvs:
        if argv[2] == "run":
            commands.setdefault(argv[argv.index("--lane") + 1], []).append(
                " ".join(argv)
            )
    got = {
        "events": {o.lane: o.events for o in result.outcomes},
        "commands": commands,
        "briefs": {
            name: [t.replace(DISPATCH["rules"], RULES_TOKEN) for t in texts]
            for name, texts in stages.briefs.items()
        },
        "receipts": [
            {
                "lane": o.lane,
                "kind": o.kind,
                "repo": o.repo,
                "prs": o.prs,
                "exit_code": o.exit_code,
                "status": o.status,
                "host": o.host,
                "retried_after": o.retried_after.model_dump()
                if o.retried_after
                else None,
            }
            for o in result.outcomes
        ],
        "deferred": result.deferred,
    }
    return got, result, slept


def test_the_recorded_sweeps_cover_every_branch() -> None:
    assert len(CASES) >= 50
    seen = {e for c in CASES for v in c["expected"]["events"].values() for e in v}
    assert {"start", "start-other-host"} <= seen
    assert any(e.startswith("wait:") for e in seen)
    assert any(c["expected"]["deferred"] for c in CASES)
    kinds = {r["kind"] for c in CASES for r in c["expected"]["receipts"]}
    assert kinds == {"diagnose", "escalation", "land-chain-head", "fix"}


@pytest.mark.parametrize("index", range(len(CASES)))
def test_the_sweep_matches_the_workflow(index: int, tmp_path: Path) -> None:
    case = CASES[index]
    got, _, _ = _sweep(case, tmp_path)
    expected = case["expected"]
    assert got["events"] == expected["events"]
    assert got["commands"] == expected["commands"]
    assert got["briefs"] == expected["briefs"]
    assert got["receipts"] == expected["receipts"]
    assert got["deferred"] == expected["deferred"]


@pytest.mark.parametrize("index", range(len(CASES)))
def test_the_plan_is_the_old_plan_and_each_wait_is_slept(
    index: int, tmp_path: Path
) -> None:
    case = CASES[index]
    _, result, slept = _sweep(case, tmp_path)
    assert result.plan is not None
    got = result.plan.model_dump(exclude_unset=True)
    expected = RECORDED["scenarios"][case["scenario"]]["expected"]["plan"]
    assert {k: got[k] for k in ("lab_only", "lanes", "skipped")} == {
        k: expected[k] for k in ("lab_only", "lanes", "skipped")
    }
    waits = [
        float(e.split(":")[1]) * 60
        for events in case["expected"]["events"].values()
        for e in events
        if e.startswith("wait:")
    ]
    assert sorted(slept) == sorted(waits)


def test_a_broken_brief_fails_the_cases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positive control: a brief that drops the no-merge rule disagrees with the workflow."""
    original = handler_merge_sweep_brief.STANDING_RULES
    monkeypatch.setattr(
        handler_merge_sweep_brief,
        "STANDING_RULES",
        original.replace("Premise", "Premise."),
    )
    misses = sum(
        _sweep(c, tmp_path / str(i))[0]["briefs"] != c["expected"]["briefs"]
        for i, c in enumerate(CASES)
    )
    assert misses > 0


def test_a_swapped_retry_decision_fails_the_cases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positive control: a retry that never waits disagrees with the workflow's events."""
    from omnimarket.nodes.node_merge_sweep_plan_compute.handlers import (
        handler_merge_sweep_retry,
    )

    monkeypatch.setattr(handler_merge_sweep_retry, "EXIT_NO_HOST", 74)
    misses = sum(
        _sweep(c, tmp_path / str(i))[0]["events"] != c["expected"]["events"]
        for i, c in enumerate(CASES)
    )
    assert misses > 0
