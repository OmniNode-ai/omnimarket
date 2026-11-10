# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab fill nodes decide what lab-fill.js decided, cell by cell, on recorded ticks (OMN-22986).

The fixture holds each recorded tick: the driver's args, the host readings or the planned
capacity, the candidates and the fenced tickets, beside what the current lab-fill.js pure block
answered under node (planCapacity, selectWork, dispatchCommand). Each tick is replayed through
the packaged capacity and dispatch-plan handlers and every cell of the answer is compared.
"""

from __future__ import annotations

import json
import re
import shlex
import typing
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_launch import (
    runner_args,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
    HandlerLabFillCapacity,
    HandlerLabFillDispatchPlan,
    HandlerLabFillLaneRender,
)


def _request(handler: Any, payload: dict[str, Any]) -> Any:
    """Validate a payload into the request type the handler's own `handle` declares.

    Reading the type from the handler keeps this test off the plan-compute node's
    private models package.
    """
    return typing.get_type_hints(type(handler).handle)["request"].model_validate(
        payload
    )


FIXTURE = Path(__file__).parent / "fixtures" / "recorded_ticks.json"
TICKS: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["ticks"]
_FIRE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})\d{2}Z-")


def _run_key(args: dict[str, Any]) -> str:
    fire = _FIRE.match(str(args.get("fireId", "")))
    return (
        f"{fire[1]}-{fire[2]}-{fire[3]}T{fire[4]}{fire[5]}Z"
        if fire
        else str(args["date"])
    )


def node_config(args: dict[str, Any]) -> dict[str, Any]:
    """The node's run configuration, read from the driver's args and nothing else."""
    conf = args["config"]
    scope = conf["ticketScope"]
    priority = conf.get("priority")
    config: dict[str, Any] = {
        "parent_ticket": scope["parentTicket"],
        "milestone": scope["milestone"],
        "run_key": _run_key(args),
        "project_id": scope.get("projectId", ""),
        "operator_id": scope.get("operatorId", ""),
        "pr_repos": conf["prRepos"],
        "exclude_areas": scope["excludeAreas"],
        "defect_labels": conf["defects"]["labels"],
        "defect_terms": conf["defects"]["terms"],
        "max_lanes": conf["maxLanes"],
    }
    for key, field in (("mvpRepos", "mvp_repos"), ("processRepos", "process_repos")):
        if priority and key in priority:
            config[field] = priority[key]
    return config


def node_answer(tick: dict[str, Any]) -> dict[str, Any]:
    """What the packaged handlers answer for one recorded tick."""
    if "readings" in tick:
        capacity = HandlerLabFillCapacity()
        request = _request(
            capacity,
            {
                "readings": tick["readings"],
                "policy": tick["policy"],
                "max_lanes": tick["args"]["config"]["maxLanes"],
            },
        )
        result = capacity.handle(request)
        return {
            "capacity": result.model_dump(
                mode="json", by_alias=True, exclude_unset=True
            )
        }
    planner = HandlerLabFillDispatchPlan()
    plan = _request(
        planner,
        {
            "candidates": tick["candidates"],
            "capacity": tick["capacity"],
            "config": node_config(tick["args"]),
            "fenced": tick["fenced"],
        },
    )
    selection = planner.handle(plan)
    dumped = selection.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return {
        "selection": dumped,
        "send": [node_send_flags(item, tick) for item in dumped["dispatch"]],
    }


def render_config(args: dict[str, Any]) -> dict[str, Any]:
    """The node's lane render configuration, read from the driver's args and nothing else."""
    conf = args["config"]
    scope = conf["ticketScope"]
    config: dict[str, Any] = {
        "pillar": conf["pillar"],
        "parent_lane": conf["parentLane"],
        "parent_ticket": scope["parentTicket"],
        "run_key": _run_key(args),
        "authority_ruling": conf["authority"]["ruling"],
        "authority_lane": conf["authority"]["lane"],
        "landing_lane": conf.get("landingLane", "landing-controller"),
    }
    return config


def js_send_flags(command: str) -> dict[str, str]:
    """The runner flags of a lab-fill.js dispatch command, its priority class env var as a flag."""
    tokens = shlex.split(command)
    flags: dict[str, str] = {}
    for token in tokens:
        if token.startswith("ONEX_LANE_PRIORITY_CLASS="):
            flags["--priority-class"] = token.split("=", 1)[1]
    run = tokens.index("run")
    rest = tokens[run + 1 :]
    index = 0
    while index < len(rest):
        flag = rest[index]
        if index + 1 < len(rest) and not rest[index + 1].startswith("--"):
            flags[flag] = rest[index + 1]
            index += 2
        else:
            flags[flag] = ""
            index += 1
    return flags


def node_send_flags(item: dict[str, Any], tick: dict[str, Any]) -> dict[str, str]:
    """The runner flags the node's lane render and launch build for one planned lane."""
    render = HandlerLabFillLaneRender()
    rendered = render.handle(
        _request(render, {"item": item, "config": render_config(tick["args"])})
    )
    argv = runner_args(rendered.launch, f"$BRIEF_DIR/{item['lane']}.md")
    flags: dict[str, str] = {}
    index = 1
    while index < len(argv):
        if index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            flags[argv[index]] = argv[index + 1]
            index += 2
        else:
            flags[argv[index]] = ""
            index += 1
    return flags


def cells(value: Any, path: str = "") -> dict[str, Any]:
    """Every leaf of a JSON value keyed by its path; an empty container is a leaf."""
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for key, item in value.items():
            out.update(cells(item, f"{path}.{key}" if path else str(key)))
        return out
    if isinstance(value, list) and value:
        out = {}
        for index, item in enumerate(value):
            out.update(cells(item, f"{path}[{index}]"))
        return out
    return {path: value}


_MISSING = "<absent>"


def divergent_cells(recorded: Any, replayed: Any) -> list[tuple[str, Any, Any]]:
    """Each cell where the two answers differ, as (path, lab-fill.js, node)."""
    left, right = cells(recorded), cells(replayed)
    return [
        (path, left.get(path, _MISSING), right.get(path, _MISSING))
        for path in sorted(left.keys() | right.keys())
        if left.get(path, _MISSING) != right.get(path, _MISSING)
    ]


def _recorded_answer(tick: dict[str, Any]) -> dict[str, Any]:
    recorded = tick["recorded"]
    answer = {
        key: recorded[key] for key in ("capacity", "selection") if key in recorded
    }
    if "commands" in recorded:
        answer["send"] = [js_send_flags(c) for c in recorded["commands"]]
    return answer


def test_fixture_holds_enough_recorded_ticks() -> None:
    names = [t["name"] for t in TICKS]
    assert len(names) == len(set(names))
    assert sum(1 for n in names if n.startswith("capacity/")) > 15
    assert sum(1 for n in names if n.startswith("tick/")) > 15
    assert sum(1 for n in names if n.startswith("ext/")) >= 9


@pytest.mark.parametrize("tick", TICKS, ids=[t["name"] for t in TICKS])
def test_parity_with_lab_fill_js(tick: dict[str, Any]) -> None:
    diff = divergent_cells(_recorded_answer(tick), node_answer(tick))
    assert diff == [], f"{len(diff)} cell(s) differ from lab-fill.js: {diff[:6]}"


def test_planted_divergence_in_the_recording_turns_the_replay_red() -> None:
    """Control: one cell of the recorded answer changed is named, and only that cell."""
    tick = next(
        t
        for t in TICKS
        if t["name"].startswith("tick/") and t["recorded"]["selection"]["dispatch"]
    )
    answer = _recorded_answer(tick)
    assert divergent_cells(answer, node_answer(tick)) == []
    planted = json.loads(json.dumps(answer))
    planted["selection"]["dispatch"][0]["host"] = "planted-host"
    planted["send"][0]["--lane"] = "planted-lane"
    assert [path for path, _, _ in divergent_cells(planted, node_answer(tick))] == [
        "selection.dispatch[0].host",
        "send[0].--lane",
    ]


def test_planted_divergence_in_the_node_input_turns_the_replay_red() -> None:
    """Control: the same recorded tick replayed under a changed node setting must differ."""
    tick = next(t for t in TICKS if t["name"] == "ext/priority-class-order")
    assert divergent_cells(_recorded_answer(tick), node_answer(tick)) == []
    planted = json.loads(json.dumps(tick))
    planted["args"]["config"]["priority"] = {
        "mvpRepos": ["repo_proc"],
        "processRepos": ["omnimarket"],
    }
    diff = divergent_cells(_recorded_answer(tick), node_answer(planted))
    assert diff, "a changed priority class setting must change the node's answer"
    assert any(
        path.endswith(".priorityClass") or ".lane" in path for path, _, _ in diff
    )
