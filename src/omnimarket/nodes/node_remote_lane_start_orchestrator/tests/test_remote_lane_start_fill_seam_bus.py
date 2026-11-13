# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill's filled runner_command crosses the bus and meets the entry's parser (OMN-22848).

The seam under proof joins the floor fill (``node_floor_fill_orchestrator``, which fills the
five placeholders ``{lane} {key} {ticket} {attempt} {brief}`` into ``floor_fill.runner_command``) to
this entry (``runtime_remote_lane_start.parser``). Producer side, in the test process: the
fill's example overlay's ``runner_command`` with the placeholders filled from
``FILL_PLACEHOLDERS``, plus the fill's placeholder set, goes onto the bus under test as the
JSON request; a case may carry a ``rename`` mapping that replants one of the argv's flags
first. Consumer side, the entry's own ``parser()`` answers a projection: the flags the fill
passed that the entry does not know (``unknown_to_entry``), the entry's required flags the
fill did not pass (``unbridged_required``, read from the parser's refusal of an empty
command line) and the sorted placeholder names the fill filled. The as-shipped case is
pinned green; the planted rename ``--lane`` -> ``--lane-name`` is the failing control that
must turn the pin red. The in-memory bus leg runs everywhere. The Redpanda leg runs against
the broker the lab names in ``FLOOR_FILL_PARITY_BOOTSTRAP``:

    FLOOR_FILL_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "floor_fill_parity and kafka" -k fill_seam -q
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import uuid
from typing import Any

import pytest
import yaml

from omnimarket.models.model_scenario_row import ModelScenarioRow
from omnimarket.models.model_scenario_run import ModelScenarioRun
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.runtime_remote_lane_start import (
    parser,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_bus import (
    BOOTSTRAP_ENV,
    Bus,
    InmemoryBus,
    RedpandaBus,
    topic_prefix,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.test_remote_lane_start_seams import (
    FILL_OVERLAY,
    FILL_PLACEHOLDERS,
)

pytestmark = [pytest.mark.unit, pytest.mark.floor_fill_parity]

OVERLAY = ModelFactoryBindingsOverlay.model_validate(
    {
        "profiles": [{"id": "inmemory", "baseline": True}, {"id": "redpanda"}],
        "bindings": {"inmemory": {"BUS": "inmemory"}, "redpanda": {"BUS": "redpanda"}},
        "proof_bindings": ["inmemory", "redpanda"],
    }
)

#: Measured 2026-11-10 against omnimarket dev, the fill passed --lane --key --ticket --attempt and the six required
#: flags --brief --ledger --model --overlay --pool-snapshot --run-id were unbridged: the entry's parser refused a
#: floor-line start with exit 2. The fill's command now carries all six (the brief as a file the fill writes), so the
#: entry knows every flag it passes, none of its required flags is unbridged, and the fill fills five placeholders.
EXPECTED_AS_SHIPPED: dict[str, list[str]] = {
    "unknown_to_entry": [],
    "unbridged_required": [],
    "placeholders": ["attempt", "brief", "key", "lane", "ticket"],
}


def seam_cases() -> list[tuple[str, dict[str, Any]]]:
    """The two cases: the fill's command as shipped, and the planted argument rename."""
    return [
        ("00:as-shipped", {}),
        ("01:planted-rename", {"rename": {"--lane": "--lane-name"}}),
    ]


def filled_command(rename: dict[str, str] | None) -> list[str]:
    """The fill's runner_command with the placeholders filled, the rename applied."""
    overlay = yaml.safe_load(FILL_OVERLAY.read_text(encoding="utf-8"))
    argv = [
        re.sub(r"\{(\w+)\}", lambda m: FILL_PLACEHOLDERS[m.group(1)], part)
        for part in overlay["floor_fill"]["runner_command"]
    ]
    return [rename.get(part, part) for part in argv] if rename else argv


def filled_placeholders() -> list[str]:
    """The placeholder names the fill's command fills, read from the overlay text."""
    overlay = yaml.safe_load(FILL_OVERLAY.read_text(encoding="utf-8"))
    raw = " ".join(overlay["floor_fill"]["runner_command"])
    return sorted(set(re.findall(r"\{(\w+)\}", raw)))


def producer_request(case: dict[str, Any]) -> dict[str, Any]:
    """The JSON request the fill side sends: the filled argv plus its placeholder set."""
    return {
        "argv": filled_command(case.get("rename")),
        "placeholders": filled_placeholders(),
    }


def entry_required_flags() -> frozenset[str]:
    """The flags the parser demands, read from its own refusal of an empty command line."""
    refusal = io.StringIO()
    with contextlib.redirect_stderr(refusal), pytest.raises(SystemExit):
        parser().parse_args([])
    listed = refusal.getvalue().split("the following arguments are required:")[1]
    return frozenset(flag.strip() for flag in listed.split(","))


def entry_known_flags() -> frozenset[str]:
    return frozenset(re.findall(r"--[a-z][a-z-]*", parser().format_help()))


def supplied_flags(argv: list[str]) -> frozenset[str]:
    return frozenset(part for part in argv if part.startswith("--"))


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """The consumer side: judge the fill's argv against the entry's own parser."""
    passed = supplied_flags(payload["request"]["argv"])
    return {
        "id": payload["id"],
        "output": {
            "unknown_to_entry": sorted(passed - entry_known_flags()),
            "unbridged_required": sorted(entry_required_flags() - passed),
            "placeholders": sorted(payload["request"]["placeholders"]),
        },
    }


def in_process() -> dict[str, str]:
    return {
        name: json.dumps(
            decide({"id": name, "request": producer_request(case)})["output"],
            sort_keys=True,
        )
        for name, case in seam_cases()
    }


async def run_over(bus: Bus, profile: str, binding: str) -> ModelScenarioRun:
    await bus.start()
    try:
        rows = []
        for name, case in seam_cases():
            reply = await bus.call(
                {"id": uuid.uuid4().hex, "request": producer_request(case)}
            )
            rows.append(
                ModelScenarioRow(
                    key=name, content=json.dumps(reply["output"], sort_keys=True)
                )
            )
    finally:
        await bus.stop()
    return ModelScenarioRun(
        profile_id=profile,
        binding=binding,
        proof_eligible=True,
        verdicts=("PASS",),
        rows=tuple(rows),
        counts={"decisions": len(rows)},
    )


def make_bus(binding: str) -> Bus:
    prefix = topic_prefix("lane-start-fill-seam")
    if binding == "inmemory":
        return InmemoryBus(decide, prefix)
    return RedpandaBus(decide, prefix, os.environ[BOOTSTRAP_ENV])


def seam_run(*, profiles: tuple[str, ...]) -> tuple[ModelScenarioRun, ...]:
    chosen = ModelFactoryBindingsOverlay.model_validate(
        {
            "profiles": [p.model_dump() for p in OVERLAY.profiles if p.id in profiles],
            "bindings": {p: OVERLAY.bindings[p] for p in profiles},
            "proof_bindings": list(OVERLAY.proof_bindings),
        }
    )
    return tuple(
        asyncio.run(
            run_over(
                make_bus(chosen.binding_id(profile.id, "BUS")),
                profile.id,
                chosen.binding_id(profile.id, "BUS"),
            )
        )
        for profile in chosen.profiles
    )


def test_fill_seam_as_shipped_projection_matches_the_pin_in_process() -> None:
    rows = {k: json.loads(v) for k, v in in_process().items()}
    assert rows["00:as-shipped"] == EXPECTED_AS_SHIPPED


def test_fill_seam_planted_rename_control_turns_the_pin_red() -> None:
    """Failing control: a renamed argument turns the pin red while 00 stays green."""
    rows = {k: json.loads(v) for k, v in in_process().items()}
    assert rows["00:as-shipped"] == EXPECTED_AS_SHIPPED
    planted = rows["01:planted-rename"]
    assert planted != EXPECTED_AS_SHIPPED
    assert planted["unknown_to_entry"] == ["--lane-name"]
    assert "--lane" in planted["unbridged_required"]


def test_fill_seam_a_dropped_required_flag_control_turns_the_pin_red() -> None:
    """Failing control for the bridge: the command without ``--model`` (one of the six) leaves it unbridged."""
    argv = list(filled_command(None))
    at = argv.index("--model")
    dropped = argv[:at] + argv[at + 2 :]
    output = decide({"id": "x", "request": {"argv": dropped, "placeholders": []}})[
        "output"
    ]
    assert output["unbridged_required"] == ["--model"]
    assert output != {**EXPECTED_AS_SHIPPED, "placeholders": []}


def test_fill_seam_in_memory_bus_projection_matches_the_in_process_one() -> None:
    [run] = seam_run(profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process()


@pytest.mark.kafka
@pytest.mark.integration
def test_fill_seam_redpanda_bus_projection_matches_the_in_memory_one() -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    runs = seam_run(profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process()
    assert len(runs[1].rows) == len(seam_cases())
