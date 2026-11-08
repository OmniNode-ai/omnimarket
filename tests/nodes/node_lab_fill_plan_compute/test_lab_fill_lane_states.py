# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the lane-states operation of the lab-fill plan node (OMN-23025).

The golden input is the lanes cell of the lab fill's 2026-11-08T09:10Z STATUS row exactly as
the ledger holds it. That row read states=placed:8,never-started:2,preparing:1 and
refusals=none; one lane (omn22950) read pending, and its CLAIM row on a host is stamped
09:24:42Z, so the settled row is placed:9 with the two lanes that never started named.
"""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
    HandlerLabFillLaneStates,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillLaneStatesRequest,
)

RECORDED_LANES_CELL = (
    "lab-fill-omn22053-11080910@h051:placed,"
    "lab-fill-omn22567-11080910@h101:placed,"
    "lab-fill-omn22796-11080910@h201:placed,"
    "lab-fill-omn22908-11080910@h051:skipped:brief-check-refused,"
    "lab-fill-omn22938-11080910@h051:placed,"
    "lab-fill-omn22940-11080910@h101:placed,"
    "lab-fill-omn22950-11080910@h051:pending,"
    "lab-fill-omn23009-11080910@h201:placed,"
    "lab-fill-omn23019-11080910@h051:placed,"
    "lab-fill-omn23021-11080910@h051:skipped:brief-rules-unreadable,"
    "lab-fill-omn23059-11080910@h101:placed"
)
CLAIMED = ("lab-fill-omn22950-11080910",)


def placements(cell: str) -> tuple[dict[str, object], ...]:
    """lane@host:outcome entries, the outcome keeping its own colons."""
    out: list[dict[str, object]] = []
    for entry in cell.split(","):
        lane_host, _, outcome = entry.partition(":")
        lane, _, host = lane_host.partition("@")
        out.append({"lane": lane, "host": host, "outcome": outcome})
    return tuple(out)


def settle(cell: str = RECORDED_LANES_CELL, claimed: tuple[str, ...] = CLAIMED) -> Any:
    return HandlerLabFillLaneStates().handle(
        ModelLabFillLaneStatesRequest(
            placements=placements(cell), claimed_lanes=claimed
        )
    )


def test_golden_recorded_row_settles_to_final_placed_and_names_the_lanes() -> None:
    result = settle()
    assert result.states_cell == "states=placed:9,never-started:2"
    assert result.placed == 9
    assert result.never_started == (
        "lab-fill-omn22908-11080910",
        "lab-fill-omn23021-11080910",
    )
    assert result.refusals_cell == (
        "refusals=lab-fill-omn22908-11080910@h051:brief-check-refused,"
        "lab-fill-omn23021-11080910@h051:brief-rules-unreadable"
    )


def test_without_a_claim_the_pending_lane_is_still_preparing_and_not_a_refusal() -> (
    None
):
    result = settle(claimed=())
    assert result.states_cell == "states=placed:8,never-started:2,preparing:1"
    assert result.placed == 8
    assert "omn22950" not in result.refusals_cell


def test_the_recorded_row_before_the_change_named_no_lane() -> None:
    """The old row: never-started:2 counted, refusals=none. The new cells are never that while a lane never started."""
    assert settle().refusals_cell != "refusals=none"


def test_error_chain_every_failure_outcome_is_named_with_its_reason() -> None:
    result = settle(
        "a@h1:refused,b@h1:engine-failed:codex-missing,c@h2:deferred:pinned-host-refused,"
        "d@h2:not-started,e@h3:skipped:lane-timeout:40s,f@h3:strange,g@h3:placed-elsewhere"
    )
    assert result.states_cell == "states=refused:3,never-started:2,other:1,placed:1"
    assert result.refusals_cell == (
        "refusals=a@h1:refused,b@h1:engine-failed:codex-missing,"
        "c@h2:deferred:pinned-host-refused,d@h2:not-started,"
        "e@h3:lane-timeout:40s,f@h3:other:strange"
    )
    assert result.placed == 1


def test_error_chain_an_empty_run_reads_none_not_a_blank() -> None:
    result = HandlerLabFillLaneStates().handle(ModelLabFillLaneStatesRequest())
    assert (result.states_cell, result.refusals_cell, result.placed) == (
        "states=none",
        "refusals=none",
        0,
    )


def test_error_chain_a_pipe_in_a_reason_cannot_open_a_ledger_cell() -> None:
    result = HandlerLabFillLaneStates().handle(
        ModelLabFillLaneStatesRequest(
            placements=({"lane": "x", "outcome": "skipped:a|b\nc"},)
        )
    )
    assert "|" not in result.refusals_cell
    assert "\n" not in result.refusals_cell


def test_the_operation_is_routed_in_the_packaged_contract() -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_lab_fill_plan_compute")
        .joinpath("contract.yaml")
        .read_text()
    )
    entry = next(
        e
        for e in contract["handler_routing"]["handlers"]
        if e["operation"] == "settle_lab_fill_lane_states"
    )
    handler = entry["handler"]
    module = importlib.import_module(handler["module"])
    assert getattr(module, handler["name"]) is HandlerLabFillLaneStates
