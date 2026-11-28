# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill dispatch plan takes its engine from the routing order node's answer (OMN-20287).

Exemplar: test_lab_fill_pr_land_dispatch.py builds a plan request around one host. The routing
orders here are what node_delegation_routing_order_compute answers (rung names: ladder, codex,
claude-glm, lab-agent, claude), injected through the request's declared ``routing_orders`` input.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_dispatch_plan import (
    HandlerLabFillDispatchPlan,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillCapacityResult,
    ModelLabFillDispatchPlanRequest,
    ModelLabFillHostCapacity,
    ModelLabFillPlanConfig,
)

pytestmark = pytest.mark.unit

ORDERS = {
    "document": ("ladder", "claude-glm", "claude"),
    "test": ("ladder", "claude-glm", "claude"),
    "code_generation": ("ladder", "codex", "claude-glm", "claude"),
    "refactor": ("ladder", "claude"),
}


def _item(ticket: str, **over: Any) -> dict[str, object]:
    return {
        "kind": "ticket",
        "ticket": ticket,
        "repo": "omnimarket",
        "title": "change",
        "under_parent": True,
        **over,
    }


def _plan(
    candidates: list[dict[str, object]],
    *,
    codex: bool = True,
    claude: bool = True,
    engine_capacity: dict[str, tuple[str, ...]] | None = None,
    orders: dict[str, tuple[str, ...]] | None = None,
) -> Any:
    host = ModelLabFillHostCapacity(
        name="h201",
        lanes=8,
        idle_lanes=8,
        runner_slots=8,
        cap_bound=False,
        codex=codex,
        claude=claude,
        running=0,
        load_per_core=0.1,
        reason="",
    )
    return HandlerLabFillDispatchPlan().handle(
        ModelLabFillDispatchPlanRequest(
            candidates=tuple(candidates),
            capacity=ModelLabFillCapacityResult(hosts=(host,), free=8, budget=8),
            config=ModelLabFillPlanConfig(
                parent_ticket="OMN-1", milestone="m", run_key="2026-10-10", max_lanes=8
            ),
            routing_orders=ORDERS if orders is None else orders,
            engine_capacity=engine_capacity
            if engine_capacity is not None
            else {"code-edit": ("h201",), "glm": ("h201",)},
        )
    )


def _only(result: Any) -> Any:
    assert len(result.dispatch) == 1, result.deferred
    return result.dispatch[0]


def test_docs_task_routes_to_local_engine() -> None:
    item = _only(_plan([_item("OMN-10", title="docs: explain the node")]))
    assert item.engine == "code-edit"
    assert item.routing.chosen_engine == "code-edit"
    assert item.routing.requested_order == ("code-edit", "glm", "sonnet")


def test_test_task_routes_to_glm_when_local_full() -> None:
    result = _plan(
        [_item("OMN-11", title="add a unit test for the parser")],
        engine_capacity={"glm": ("h201",)},
    )
    item = _only(result)
    assert item.engine == "glm"
    assert item.routing.skipped[0].engine == "code-edit"
    assert item.routing.skipped[0].reason == "no-capacity"


def test_self_contained_code_routes_to_codex() -> None:
    # The local lab model and GLM are full (no engine_capacity), so the order's codex rung takes it.
    item = _only(
        _plan(
            [_item("OMN-12", self_contained_code=True)],
            engine_capacity={},
        )
    )
    assert item.engine == "codex"
    assert "codex" in item.route


def test_pr_fix_routes_to_sonnet() -> None:
    item = _only(
        _plan(
            [_item("OMN-13", kind="pr-red", pr="omnimarket#5")],
            engine_capacity={},
        )
    )
    assert item.engine == "sonnet"


def test_band_b4_and_b5_route_to_opus() -> None:
    for ticket, band in (("OMN-14", "B4"), ("OMN-15", "B5")):
        item = _only(_plan([_item(ticket, band=band)]))
        assert item.engine == "opus", band
        assert "model=opus" in item.route
        assert item.routing.reason.startswith("band-")


def test_full_engine_falls_to_next_with_reason_recorded() -> None:
    # codex is first with capacity gone: the order's next engine takes the lane.
    item = _only(
        _plan(
            [_item("OMN-16", self_contained_code=True)],
            codex=False,
            engine_capacity={},
        )
    )
    assert item.engine == "sonnet"
    # The order opens on the local lab model (the routing node refuses a chain that does not), which is full.
    assert [(k.engine, k.reason) for k in item.routing.skipped] == [
        ("code-edit", "no-capacity"),
        ("codex", "no-capacity"),
        ("glm", "no-capacity"),
    ]
    assert item.routing.reason.startswith("fell-to-sonnet:")


def test_codex_for_a_pr_is_not_placeable_and_falls_on() -> None:
    result = _plan(
        [_item("OMN-17", kind="pr-red", pr="omnimarket#6", self_contained_code=True)],
        orders={
            "refactor": ("codex", "claude"),
            "code_generation": ("codex", "claude"),
        },
    )
    item = _only(result)
    assert item.engine == "sonnet"
    assert item.routing.skipped[0].reason == "runner-cannot-place:pr-branch"


def test_unplaceable_rung_is_recorded_not_dropped() -> None:
    result = _plan(
        [_item("OMN-18")], orders={"refactor": ("cheap-frontier-x", "claude")}
    )
    item = _only(result)
    assert item.engine == "sonnet"
    assert item.routing.skipped[0].reason == "runner-cannot-place:unknown-engine"


def test_event_carries_requested_order_engine_and_reason() -> None:
    result = _plan([_item("OMN-19", title="docs: write guide")])
    (event,) = result.routing_events
    assert event.event_type == "lab-fill-routing-decision"
    assert event.lane == result.dispatch[0].lane
    assert event.ticket == "OMN-19"
    assert event.chosen_engine == "code-edit"
    assert event.requested_order == ("code-edit", "glm", "sonnet")
    assert event.reason == "first-with-capacity"


def test_no_order_keeps_the_fill_engine_choice() -> None:
    result = _plan([_item("OMN-20", self_contained_code=True)], orders={})
    item = _only(result)
    assert item.engine == "codex"
    assert item.routing is None
    assert result.routing_events == ()
