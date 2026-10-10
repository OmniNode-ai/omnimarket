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
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.lab-fill-plan-requested.v1",
        "onex.intent.platform.runtime-tick.v1",
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.lab-fill-plan-decided.v1",
        "onex.evt.omnimarket.lab-fill-plan-failed.v1",
    ]
    assert contract["terminal_event"] in bus["publish_topics"]
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert {e["operation"] for e in routing["handlers"]} == {
        "plan_lab_fill_capacity",
        "choose_lab_fill_candidates",
        "plan_lab_fill_dispatch",
        "decide_lab_fill_ownership",
        "render_lab_fill_lane",
        "verify_lab_fill_placement",
        "plan_lab_fill_fallbacks",
        "compose_lab_fill_status",
        "plan_lab_fill_headroom",
        "verify_lab_fill_fanout",
        "lab_fill.scheduled_fire",
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

    render_config = {
        "pillar": "delegation",
        "parent_lane": "parent-lane",
        "parent_ticket": CONFIG.parent_ticket,
        "run_key": CONFIG.run_key,
        "authority_ruling": "2026-09-30T18:00:00Z",
        "authority_lane": "authority-lane",
    }
    selected = plan.dispatch[0].model_dump(
        mode="json", by_alias=True, exclude_unset=True
    )
    handler, request_type, result_type = _resolve(entries["decide_lab_fill_ownership"])
    ownership = handler().handle(
        request_type.model_validate(
            {
                "lanes": [
                    {
                        key: selected[key]
                        for key in ("lane", "ticket", "pr", "repo", "kind")
                    }
                ],
                "claim_index": {},
                "ledger_claims": [],
                "now": "2026-10-03T18:10:00Z",
                "staleness_hours": 6,
                "pr_claims": {},
                "watcher_merged": {},
            }
        )
    )
    assert isinstance(ownership, result_type)
    assert [p.lane for p in ownership.proceed] == [selected["lane"]]
    assert ownership.skipped == ()

    handler, request_type, result_type = _resolve(entries["render_lab_fill_lane"])
    rendered = handler().handle(
        request_type.model_validate({"item": selected, "config": render_config})
    )
    assert isinstance(rendered, result_type)
    assert rendered.launch.lane == ownership.proceed[0].lane
    assert rendered.launch.host is None
    assert "AUTHORITY: operator RULING 2026-09-30T18:00:00Z" in rendered.brief

    dispatched = [{"lane": rendered.launch.lane, "detached": True}]
    handler, request_type, result_type = _resolve(entries["verify_lab_fill_placement"])
    placement = handler().handle(
        request_type.model_validate(
            {
                "planned": [selected],
                "dispatched": dispatched,
                "receipts": [
                    {
                        "lane": rendered.launch.lane,
                        "found": True,
                        "status": "running",
                        "host": "host_a",
                    }
                ],
            }
        )
    )
    assert isinstance(placement, result_type)
    assert placement.placements[0]["outcome"] == "placed"
    assert placement.failure is None

    handler, request_type, result_type = _resolve(entries["plan_lab_fill_fallbacks"])
    fallback = handler().handle(
        request_type.model_validate(
            {
                "planned": [selected],
                "placements": placement.placements,
                "capacity": capacity,
            }
        )
    )
    assert isinstance(fallback, result_type)
    assert fallback.fallbacks == ()

    handler, request_type, result_type = _resolve(entries["compose_lab_fill_status"])
    status = handler().handle(
        request_type.model_validate(
            {
                "config": render_config,
                "capacity": capacity,
                "selection": plan.model_dump(
                    mode="json", by_alias=True, exclude_unset=True
                ),
                "placements": placement.placements,
                "candidates": {
                    "source": chosen.source,
                    "candidates": chosen.candidates,
                    "detail": chosen.detail,
                },
                "dispatched": dispatched,
                "fallbacks": fallback.outcomes,
                "observed_at": "2026-10-03T18:11:00Z",
            }
        )
    )
    assert isinstance(status, result_type)
    assert "dispatched=1" in status.cells
    assert "placed=1" in status.cells
    assert status.idle["free_slots"] == 2
    assert status.idle["idle_alarm"] is False


def test_golden_codex_setup_failure_gets_one_fallback_and_status() -> None:
    """A real fallback uses the same contract-bound render and placement decisions."""
    import json
    from pathlib import Path

    cases = json.loads(
        (Path(__file__).parent / "fixtures" / "parity_cases_outcomes.json").read_text()
    )["cases"]
    entries = {e["operation"]: e for e in _contract()["handler_routing"]["handlers"]}
    inputs = next(
        c["input"] for c in cases if c["name"] == "fallback-True-codex-missing-0"
    )
    handler, request_type, result_type = _resolve(entries["plan_lab_fill_fallbacks"])
    fallback = handler().handle(request_type.model_validate(inputs))
    assert isinstance(fallback, result_type)
    assert len(fallback.fallbacks) == 1
    item = fallback.fallbacks[0]
    render_config = next(
        c["input"]["config"] for c in cases if c["op"] == "render_lab_fill_lane"
    )
    handler, request_type, result_type = _resolve(entries["render_lab_fill_lane"])
    rendered = handler().handle(request_type(item=item, config=render_config))
    assert isinstance(rendered, result_type)
    assert rendered.launch.host == fallback.hosts[item.lane] == "host_a"
    assert "FALLBACK: Codex lane lane-a ended codex-missing" in rendered.brief
    returned = {
        "lane": item.lane,
        "host": "host_a",
        "engine": "sonnet",
        "detached": True,
        "found": True,
        "status": "running",
    }
    handler, request_type, result_type = _resolve(entries["plan_lab_fill_fallbacks"])
    retry = handler().handle(
        request_type.model_validate({**inputs, "returned": [returned]})
    )
    assert isinstance(retry, result_type)
    assert retry.outcomes[0]["outcome"] == "placed"
    handler, request_type, result_type = _resolve(entries["compose_lab_fill_status"])
    status = handler().handle(
        request_type.model_validate(
            {
                "config": render_config,
                "capacity": inputs["capacity"],
                "selection": {"budget": 3, "dispatch": inputs["planned"]},
                "placements": [
                    {**inputs["placements"][0], "host": "host_a", "engine": "codex"}
                ],
                "candidates": {
                    "source": "enumerate",
                    "candidates": [{"kind": "ticket"}],
                },
                "dispatched": [{"lane": "lane-a", "detached": True}],
                "fallbacks": retry.outcomes,
                "observed_at": "2026-10-08T18:11:00Z",
            }
        )
    )
    assert isinstance(status, result_type)
    assert "dispatched=2" in status.cells
    assert status.idle["dispatched"] == 1
    assert status.idle["free_slots"] == 4
