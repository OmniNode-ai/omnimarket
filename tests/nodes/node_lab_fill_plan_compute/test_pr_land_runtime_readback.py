# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pre-merge pr-land readback through packaged operation contracts (OMN-20864).

The hosted evidence runner has no deployed lane container. Exercise the head's
runtime handlers in process, resolving them from the contracts and validating
each JSON hand-off, rather than importing code in a container after merge.
"""

from __future__ import annotations

import importlib
import json
import socket
from importlib.resources import files
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter

pytestmark = pytest.mark.unit

NOW = "2026-10-10T09:30:00Z"
PR = "repo_a#7"
HOLD = "2026-10-10T09:00:00Z | HOLD | id=readback-hold | pr=repo_a#7"


def _resolve(path: str) -> Any:
    module, _, name = path.rpartition(".")
    return getattr(importlib.import_module(module), name)


def _invoke(node: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{node}").joinpath("contract.yaml").read_text()
    )
    entry = next(
        entry
        for entry in contract["handler_routing"]["handlers"]
        if entry["operation"] == operation
    )
    handler = entry["handler"]
    handler_type = getattr(importlib.import_module(handler["module"]), handler["name"])
    models = entry if "input_model" in entry else contract["handler"]
    request = TypeAdapter(_resolve(models["input_model"])).validate_python(payload)
    result = handler_type().handle(request)
    adapter = TypeAdapter(_resolve(models["output_model"]))
    wire = adapter.dump_python(adapter.validate_python(result), mode="json")
    assert isinstance(wire, dict)
    return wire


@pytest.mark.parametrize("hold_state", ["clear", "held", "unreadable"])
def test_pr_land_runtime_readback(hold_state: str) -> None:
    facts = {
        "prs": [{"pr": PR, "head_sha": "a" * 40, "landing_state": "PARKED"}],
        "holds": {
            "source": "readback-fixture",
            "read": hold_state != "unreadable",
            "lines": [HOLD] if hold_state == "held" else [],
            "error": "fixture unavailable" if hold_state == "unreadable" else "",
        },
    }
    selected = _invoke(
        "node_lab_fill_selection_compute",
        "select_lab_fill_work",
        {
            "now": NOW,
            "candidates": [],
            "ledger_lines": [],
            "deployment": {
                "companion_repositories": [],
                "not_work_repositories": [],
                "document_repositories": [],
            },
            "idle_slots": 1,
            "pr_land": facts,
        },
    )["pr_land"]
    reading = "h101:cores=32,load1=4.0,free_gb=64.0,placed=1,cap=2,slots=1,login=claude"
    tick = _invoke(
        "node_throughput_tick_decision_compute",
        "decide_throughput_tick",
        {
            "now": NOW,
            "launchctl": "not_loaded",
            "pr_land": facts,
            "lab_headroom": {
                "hosts": [{"name": "h101", "parses": {reading: {"parsed": True}}}],
                "receipts": [
                    {
                        "started_at": "2026-10-10T09:29:00Z",
                        "host": "h101",
                        "status": "running",
                        "pid_alive": True,
                        "readings": [reading],
                    }
                ],
            },
        },
    )["pr_land"]
    assert selected == tick
    if hold_state != "clear":
        assert selected["dispatch"] == []
        if hold_state == "held":
            assert selected["decisions"][0]["hold_id"] == "readback-hold"
            assert selected["decisions"][0]["reason"] == "held"
        else:
            assert selected["failure"] == "hold-source-unreadable"
        print(
            "LAB_PR_LAND_READBACK "
            + json.dumps(
                {
                    "host": socket.gethostname(),
                    "hold_state": hold_state,
                    "plan": selected,
                }
            )
        )
        return

    assert [item["pr"] for item in selected["dispatch"]] == [PR]
    dispatch = selected["dispatch"][0]
    plan = _invoke(
        "node_lab_fill_plan_compute",
        "plan_lab_fill_dispatch",
        {
            "candidates": [{**dispatch, "ticket": "OMN-20864"}],
            "capacity": {
                "hosts": [
                    {
                        "name": "h101",
                        "lanes": 1,
                        "idle_lanes": 1,
                        "runner_slots": 1,
                        "cap_bound": False,
                        "codex": True,
                        "claude": True,
                        "running": 0,
                        "load_per_core": 0.1,
                        "reason": "fixture",
                    }
                ],
                "free": 1,
                "budget": 1,
            },
            "config": {
                "parent_ticket": "OMN-20864",
                "milestone": "m",
                "run_key": "2026-10-10",
            },
        },
    )
    assert len(plan["dispatch"]) == 1
    item = plan["dispatch"][0]
    rendered = _invoke(
        "node_lab_fill_plan_compute",
        "render_lab_fill_lane",
        {
            "item": item,
            "config": {
                "pillar": "landing",
                "parent_lane": "lab-fill",
                "parent_ticket": "OMN-20864",
                "run_key": "2026-10-10",
                "authority_ruling": "2026-10-10T04:08:23Z",
                "authority_lane": "readback-fixture",
            },
        },
    )
    assert item["kind"] == "pr-land"
    assert rendered["launch"]["pr"] == PR
    assert item["host"] == "h101"
    # Unpinned lanes leave the launch host to the runner's live placement.
    assert rendered["launch"]["host"] is None
    assert "TASK: Land repo_a#7 (OMN-20864) with /omni:pr-land" in rendered["brief"]
    print(
        "LAB_PR_LAND_READBACK "
        + json.dumps(
            {
                "host": socket.gethostname(),
                "hold_state": hold_state,
                "dispatch": item,
                "launch": rendered["launch"],
            }
        )
    )
