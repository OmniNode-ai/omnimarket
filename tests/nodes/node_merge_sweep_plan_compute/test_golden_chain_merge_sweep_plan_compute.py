# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and each operation executes (OMN-20676)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_merge_sweep_plan_compute.models import (
    ModelMergeSweepPlanRequest,
    ModelMergeSweepRetryRequest,
)

NAME = "node_merge_sweep_plan_compute"


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def _resolve(entry: dict[str, Any]) -> tuple[Any, Any, Any]:
    handler = entry["handler"]
    handler_type = getattr(importlib.import_module(handler["module"]), handler["name"])
    types = []
    for key in ("input_model", "output_model"):
        module, _, name = str(entry[key]).rpartition(".")
        types.append(getattr(importlib.import_module(module), name))
    return handler_type, types[0], types[1]


def test_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["side_effects"] == []
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.merge-sweep-plan-requested.v1"
    ]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.merge-sweep-planned.v1"]
    assert contract["terminal_event"] in bus["publish_topics"]
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert {e["operation"] for e in routing["handlers"]} == {
        "plan_merge_sweep_lanes",
        "decide_merge_sweep_lane_retry",
        "render_merge_sweep_lane_brief",
    }


def test_golden_chain_plans_a_sweep_then_decides_a_lane_retry() -> None:
    """A reading becomes ordered lanes under the cap; a no-host exit becomes a wait."""
    entries = {e["operation"]: e for e in _contract()["handler_routing"]["handlers"]}

    handler, request_type, result_type = _resolve(entries["plan_merge_sweep_lanes"])
    assert request_type is ModelMergeSweepPlanRequest
    planned = handler().handle(
        request_type.model_validate(
            {
                "max_lanes": 2,
                "reading": {
                    "now": "2026-10-09T09:00:00Z",
                    "load1": 48.0,
                    "cpus": 24,
                    "controller": {
                        "stalled": True,
                        "reasons": ["zero workers over the last 3 ticks"],
                    },
                    "product": {"under_floor": ["omnimarket"]},
                    "escalations": [
                        {"pr": "OmniNode-ai/omnimarket#3321", "state": "OPEN"}
                    ],
                    "reds": [
                        {
                            "repo": "omnibase_infra",
                            "number": 4308,
                            "state": "OPEN",
                            "ticket": "OMN-1",
                            "owner": {
                                "state": "stale",
                                "claim_ts": "2026-10-08T01:00:00Z",
                            },
                            "classes": [
                                {"name": "Tests", "cls": "real", "remedy": "fix"}
                            ],
                        },
                        {
                            "repo": "omniclaude",
                            "number": 7,
                            "state": "OPEN",
                            "owner": {"state": "live", "lane": "lane-x"},
                        },
                    ],
                },
            }
        )
    )
    assert isinstance(planned, result_type)
    assert planned.lab_only is True
    assert planned.load_per_core == 2.0
    assert [(lane.kind, lane.repo) for lane in planned.lanes] == [
        ("diagnose", None),
        ("escalation", "omnimarket"),
        ("fix", "omnibase_infra"),
    ]
    assert planned.lanes[0].reasons == [
        "controller: zero workers over the last 3 ticks",
        "under floor: omnimarket",
    ]
    assert planned.lanes[2].prs[0].supersedes_claim == "2026-10-08T01:00:00Z"
    assert [s.model_dump() for s in planned.skipped] == [
        {"pr": "omniclaude#7", "why": "live-owner lane=lane-x"}
    ]
    assert [lane.kind for lane in planned.dispatch] == ["diagnose", "escalation"]
    assert planned.deferred == 1

    handler, request_type, result_type = _resolve(
        entries["decide_merge_sweep_lane_retry"]
    )
    assert request_type is ModelMergeSweepRetryRequest
    waits = handler().handle(
        request_type(exit_code=75, waits_used=1, retries=3, retry_wait_min=7)
    )
    assert isinstance(waits, result_type)
    assert (waits.action, waits.wait_min) == ("wait_and_retry", 7)
    limited = handler().handle(request_type(exit_code=77))
    assert (limited.action, limited.wait_min) == ("retry_other_host", None)
    assert handler().handle(request_type(exit_code=0)).action == "accept"
