# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and each operation executes (OMN-20668)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillCandidateChoiceRequest,
    ModelLabFillCapacityRequest,
    ModelLabFillDispatchPlanRequest,
    ModelLabFillHeadroomPolicy,
    ModelLabFillPlanConfig,
)

NAME = "node_lab_fill_plan_compute"
READING = {
    "name": "host_a",
    "cores": 32,
    "load1": 4,
    "busy_cores": 2,
    "mem_avail_gb": 100,
    "runner_slots": 3,
    "login_claude": True,
    "codex_ok": False,
    "running_lanes": [],
}
CONFIG = ModelLabFillPlanConfig(
    parent_ticket="OMN-100", milestone="M1", run_key="2026-10-03T1810Z"
)


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
    assert bus["subscribe_topics"] == ["onex.cmd.omnimarket.lab-fill-plan-requested.v1"]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.lab-fill-plan-decided.v1"]
    assert contract["terminal_event"] in bus["publish_topics"]
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert {e["operation"] for e in routing["handlers"]} == {
        "plan_lab_fill_capacity",
        "choose_lab_fill_candidates",
        "plan_lab_fill_dispatch",
    }


def test_golden_chain_runs_every_operation_in_order() -> None:
    """Readings become capacity, a read becomes candidates, candidates become a lane."""
    entries = {e["operation"]: e for e in _contract()["handler_routing"]["handlers"]}

    handler, request_type, result_type = _resolve(entries["plan_lab_fill_capacity"])
    capacity = handler().handle(
        request_type(
            readings=(READING,), policy=ModelLabFillHeadroomPolicy(), max_lanes=8
        )
    )
    assert isinstance(capacity, result_type)
    assert request_type is ModelLabFillCapacityRequest
    assert (capacity.free, capacity.budget) == (3, 3)
    assert capacity.hosts[0].name == "host_a"

    candidate = {
        "kind": "ticket",
        "ticket": "OMN-7",
        "title": "Repair",
        "under_parent": True,
        "milestone": "M1",
        "repo": "repo_a",
    }
    handler, request_type, result_type = _resolve(entries["choose_lab_fill_candidates"])
    chosen = handler().handle(
        request_type(
            enumerated={"source": "enumerate", "candidates": [candidate]}, config=CONFIG
        )
    )
    assert isinstance(chosen, result_type)
    assert request_type is ModelLabFillCandidateChoiceRequest
    assert chosen.source == "enumerate"

    handler, request_type, result_type = _resolve(entries["plan_lab_fill_dispatch"])
    plan = handler().handle(
        request_type(
            candidates=chosen.candidates,
            capacity=capacity,
            config=CONFIG,
            fenced=chosen.fenced,
        )
    )
    assert isinstance(plan, result_type)
    assert request_type is ModelLabFillDispatchPlanRequest
    assert [(d.lane, d.host, d.engine) for d in plan.dispatch] == [
        ("lab-fill-omn7-10031810", "host_a", "sonnet")
    ]
    assert plan.skipped == ()
    assert plan.deferred == ()
