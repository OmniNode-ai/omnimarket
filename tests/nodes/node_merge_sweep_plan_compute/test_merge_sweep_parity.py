# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity: the node decides what the old merge-sweep tools decided on the same facts (OMN-20676).

``fixtures/parity_cases.json`` was recorded by running the old tools on generated inputs, whose
sources are named under ``recorded_from``: the lane plan from the merge-sweep skill's own
``sweep_rules.plan`` over generated readings, and the dispatch cap and the retry loop by running the
real ``merge_sweep.js`` with its ``agent()`` calls scripted (each lane's runner exit codes fed in
order, every start and every wait recorded). The expected answer in each case is the old tool's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_merge_sweep_plan_compute.handlers import (
    HandlerMergeSweepPlan,
    HandlerMergeSweepRetry,
    handler_merge_sweep_retry,
)
from omnimarket.nodes.node_merge_sweep_plan_compute.models import (
    ModelMergeSweepPlanRequest,
    ModelMergeSweepRetryRequest,
)

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "parity_cases.json").read_text()
)


def _planned(case: dict[str, Any]) -> Any:
    return HandlerMergeSweepPlan().handle(
        ModelMergeSweepPlanRequest.model_validate(case["request"])
    )


def _drive(case: dict[str, Any]) -> tuple[list[str], int]:
    """Run one lane through the node's retry decisions, feeding the recorded exit codes."""
    handler = HandlerMergeSweepRetry()
    exits = case["exits"]
    events = ["start"]
    code, nxt, waits, other = exits[0], 1, 0, False
    while True:
        decision = handler.handle(
            ModelMergeSweepRetryRequest(
                exit_code=code,
                waits_used=waits,
                host_retry_used=other,
                retries=case["retries"],
                retry_wait_min=case["retry_wait_min"],
            )
        )
        if decision.action == "wait_and_retry":
            events += [f"wait:{decision.wait_min}", "start"]
            waits += 1
        elif decision.action == "retry_other_host":
            events.append("start-other-host")
            other = True
        else:
            return events, code
        code, nxt = exits[nxt], nxt + 1


def test_the_recorded_cases_cover_every_branch() -> None:
    plans = CASES["plan"]
    assert len(plans) >= 300
    kinds = {lane["kind"] for c in plans for lane in c["expected"]["plan"]["lanes"]}
    assert kinds == {"diagnose", "escalation", "land-chain-head", "fix"}
    why = {
        s["why"].split(" ")[0] for c in plans for s in c["expected"]["plan"]["skipped"]
    }
    assert why >= {
        "out-of-scope-repo",
        "merged-or-closed",
        "live-owner",
        "owner-unread",
    }
    assert any(p["expected"]["plan"]["lab_only"] for p in plans)
    assert any(not p["expected"]["plan"]["lab_only"] for p in plans)
    assert any(c["expected"]["deferred"] > 0 for c in plans)
    assert any(
        "supersedes_claim" in pr
        for c in plans
        for lane in c["expected"]["plan"]["lanes"]
        for pr in lane["prs"]
    )
    retry = CASES["retry"]
    assert len(retry) >= 300
    assert any(c["retried"] for c in retry)
    assert any(any(e.startswith("wait") for e in c["events"]) for c in retry)
    assert {c["final_exit"] for c in retry} >= {0, 75, 77, 79}


@pytest.mark.parametrize("index", range(len(CASES["plan"])))
def test_plan_matches_the_sweep_rules_and_the_workflow_cap(index: int) -> None:
    case = CASES["plan"][index]
    result = _planned(case)
    got = result.model_dump(exclude_unset=True)
    expected = case["expected"]
    assert {k: got[k] for k in expected["plan"]} == expected["plan"]
    assert len(result.dispatch) == expected["dispatched"]
    assert result.deferred == expected["deferred"]
    assert result.dispatch == result.lanes[: expected["dispatched"]]


@pytest.mark.parametrize("index", range(len(CASES["cap"])))
def test_dispatch_cap_matches_the_workflow(index: int) -> None:
    case = CASES["cap"][index]
    reading = {
        "escalations": [
            {"pr": f"omnimarket#{i + 1}", "state": "OPEN"} for i in range(case["lanes"])
        ],
        "controller": {"stalled": False},
        "product": {"under_floor": []},
    }
    request = {"reading": reading, "max_lanes": case["max_lanes"]}
    result = _planned({"request": request})
    assert len(result.lanes) == case["lanes"]
    assert len(result.dispatch) == case["dispatched"]
    assert result.deferred == case["deferred"]


@pytest.mark.parametrize("index", range(len(CASES["retry"])))
def test_retry_matches_the_workflow(index: int) -> None:
    case = CASES["retry"][index]
    events, final = _drive(case)
    assert events == case["events"]
    assert final == case["final_exit"]


def test_a_broken_retry_fails_the_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: waiting on the wrong exit code disagrees with the workflow."""
    monkeypatch.setattr(handler_merge_sweep_retry, "EXIT_NO_HOST", 76)
    misses = sum(_drive(c) != (c["events"], c["final_exit"]) for c in CASES["retry"])
    assert misses > 0


def test_a_broken_owner_gate_fails_the_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: planning a live-owned PR disagrees with the sweep rules."""
    from omnimarket.nodes.node_merge_sweep_plan_compute.handlers import (
        handler_merge_sweep_plan,
    )

    original = handler_merge_sweep_plan._owner_gate

    def lenient(key: str, state: str, owner: Any, scope: Any, skipped: Any) -> Any:
        if owner is not None and owner.state == "live":
            owner = None
        return original(key, state, owner, scope, skipped)

    monkeypatch.setattr(handler_merge_sweep_plan, "_owner_gate", lenient)
    misses = 0
    for case in CASES["plan"]:
        got = _planned(case).model_dump(exclude_unset=True)
        misses += {k: got[k] for k in case["expected"]["plan"]} != case["expected"][
            "plan"
        ]
    assert misses > 0
