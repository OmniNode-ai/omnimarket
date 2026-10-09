# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and each operation executes (OMN-20669)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_remote_lane_close_compute.models import (
    ModelRemoteLaneResultRequest,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    ModelRemoteLanePlacementRequest,
)

NAME = "node_remote_lane_compute"
CLOSE_NAME = "node_remote_lane_close_compute"


def _contract(name: str = NAME) -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{name}").joinpath("contract.yaml").read_text()
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
        "onex.cmd.omnimarket.remote-lane-decision-requested.v1"
    ]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.remote-lane-decided.v1"]
    assert contract["terminal_event"] in bus["publish_topics"]
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert {e["operation"] for e in routing["handlers"]} == {
        "decide_remote_lane_placement",
    }


def test_close_contract_declares_its_own_bus_route() -> None:
    contract = _contract(CLOSE_NAME)
    assert contract["name"] == CLOSE_NAME
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["side_effects"] == []
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.remote-lane-close-requested.v1"
    ]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.remote-lane-close-decided.v1"]
    assert contract["terminal_event"] in bus["publish_topics"]
    assert {e["operation"] for e in contract["handler_routing"]["handlers"]} == {
        "decide_remote_lane_close",
    }


def test_golden_chain_places_a_lane_then_closes_it() -> None:
    """Readings become a host; the lane's final report becomes its closing outcome."""
    entries = {e["operation"]: e for e in _contract()["handler_routing"]["handlers"]}

    handler, request_type, result_type = _resolve(
        entries["decide_remote_lane_placement"]
    )
    assert request_type is ModelRemoteLanePlacementRequest
    reading = {
        "engines": ["claude_opus"],
        "lane_slots": 2,
        "lane_cap": 4,
        "mem_avail_gb": 64.0,
    }
    placed = handler().handle(
        request_type(
            engine="claude_opus",
            limited_hosts=("host_b",),
            readings=(
                {**reading, "name": "host_local", "local": True, "rank_free": 30.0},
                {**reading, "name": "host_a", "placed": 1, "rank_free": 10.0},
                {**reading, "name": "host_b", "rank_free": 20.0},
            ),
        )
    )
    assert isinstance(placed, result_type)
    assert (placed.host, placed.engine, placed.local) == (
        "host_a",
        "claude_opus",
        False,
    )
    assert [(v.host, v.reason) for v in placed.verdicts] == [
        ("host_local", "lab-host-first"),
        ("host_a", "chosen"),
        ("host_b", "usage-limited"),
    ]

    closing = {
        e["operation"]: e for e in _contract(CLOSE_NAME)["handler_routing"]["handlers"]
    }
    handler, request_type, result_type = _resolve(closing["decide_remote_lane_close"])
    assert request_type is ModelRemoteLaneResultRequest
    closed = handler().handle(
        request_type(
            engine_exit_code=0,
            final_message="Shipped.\nLANE_RESULT outcome=handed-off\nDELEGATION delegated=1 runs=r1",
        )
    )
    assert isinstance(closed, result_type)
    assert (closed.outcome, closed.problem) == ("handed-off", None)
    assert closed.delegation == {"delegated": "1", "runs": "r1"}


def test_codex_lane_ignores_claude_marks_and_takes_the_codex_bar() -> None:
    handler, request_type, _ = _resolve(
        {e["operation"]: e for e in _contract()["handler_routing"]["handlers"]}[
            "decide_remote_lane_placement"
        ]
    )
    placed = handler().handle(
        request_type(
            engine="codex",
            limited_hosts=("host_a",),
            auth_expired_hosts=("host_a",),
            readings=(
                {"name": "host_a", "rank_free": 5.0},
                {
                    "name": "host_b",
                    "rank_free": 50.0,
                    "codex_refusal": "NO-CODEX-LOGIN",
                },
            ),
        )
    )
    assert (placed.host, placed.engine) == ("host_a", "codex")
    assert placed.verdicts[1].reason == "codex: NO-CODEX-LOGIN"
