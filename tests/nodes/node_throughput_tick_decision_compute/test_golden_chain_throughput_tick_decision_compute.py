# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20686: the merge-throughput tick decisions, contract to bus to typed result, parity and refusals.

Parity: tests/fixtures/throughput_tick_parity.json holds cases captured by running the retired
throughput_tick.py and loops_check.py checks against on-disk fixtures (a fake launchctl and ps,
the controller's state directory, the PR watcher's state, the floors file and the landing policy),
beside the facts a caller read from the same fixtures. Golden chain: the request through the
registered contract on the in-memory bus. Error chain: a malformed request is refused with no result.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_throughput_tick_decision_compute as node_package
from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
    ModelThroughputTickResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.throughput-tick-decision-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.throughput-tick-decision-completed.v1"
PARITY = json.loads((ROOT / "tests/fixtures/throughput_tick_parity.json").read_text())
HANDLER = HandlerThroughputTickDecision()
NOW = "2026-10-09T06:00:00Z"

HEALTHY_WATCHER = {
    "last_tick": "2026-10-09T05:58:00Z",
    "operator": "op",
    "prs": {
        **{
            f"m{i}": {
                "facts": {
                    "repo": "r/a",
                    "state": "MERGED",
                    "merged_at": f"2026-10-09T05:{10 + i}:00Z",
                }
            }
            for i in range(6)
        },
        "o1": {
            "cls": "ready",
            "facts": {"repo": "r/a", "state": "OPEN", "author": "op"},
        },
    },
}


def request(**overrides: Any) -> dict[str, Any]:
    return {
        "now": NOW,
        "launchctl": "loaded",
        "launchctl_stdout": '{ "PID" = 77; };',
        "pid_etime": "02:00",
        "ticks": [
            {"tick": 5, "ts": "2026-10-09T05:55:00Z", "mode": "act", "status": "OK"}
        ],
        "heartbeat_text": "2026-10-09T05:55:00Z\n",
        "watcher_path": "/w.json",
        "watcher_state": HEALTHY_WATCHER,
        "floors_per_repo": {"r/a": 1},
        **overrides,
    }


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeThroughputTickDecisionCompute, handler_type)
    for side, model in (
        ("input_model", ModelThroughputTickRequest),
        ("output_model", ModelThroughputTickResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_throughput_tick_decision_compute"].load() is node_package


@pytest.mark.parametrize("case", PARITY["cases"], ids=lambda c: str(c["name"]))
def test_old_behavior_parity(case: dict[str, Any]) -> None:
    result = HANDLER.handle(ModelThroughputTickRequest.model_validate(case["request"]))
    assert result.model_dump() == case["expected"]


def test_parity_fixture_exercises_every_finding_class() -> None:
    lines = [line for c in PARITY["cases"] for line in c["expected"]["lines"]]
    for needle in (
        "MISSING controller",
        "UNKNOWN controller",
        "NOTE controller live: mode=act",
        "NOTE controller live: tick running as pid",
        "MISSING merges",
        "UNKNOWN merges",
        "NOTE merges last 60 min",
        "MISSING floor:",
        "UNKNOWN floor:",
        "UNKNOWN floors",
        "NOTE floors met",
        "NOTE floors do not count report-only",
        "MISSING escalated:",
        "NOTE escalated:",
    ):
        assert any(line.startswith(needle) for line in lines), needle
    statuses = {c["expected"]["status_line"].split(" ")[1] for c in PARITY["cases"]}
    assert statuses == {"OK", "STALL"}


def test_healthy_facts_give_an_ok_status_line() -> None:
    result = HANDLER.handle(ModelThroughputTickRequest.model_validate(request()))
    assert result.status_line == "THROUGHPUT OK checked=controller,merges,floors"
    assert result.checked == ["controller", "merges", "floors"]
    assert result.exit_code == 0
    assert result.lines[0].startswith(
        "NOTE controller live: tick running as pid 77 for 2 min"
    )


def test_a_controller_not_loaded_is_a_stall() -> None:
    result = HANDLER.handle(
        ModelThroughputTickRequest.model_validate(request(launchctl="not_loaded"))
    )
    assert result.exit_code == 1
    assert result.missing == ["controller"]
    assert result.status_line.startswith("THROUGHPUT STALL n=1 unknown=0: controller")


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"policy_load_error": "boom"}, "cannot load the landing policy: boom"),
        ({"policy_error": "bad file"}, "cannot read the landing policy: bad file"),
        ({"floors_per_repo": None, "floors_error": "gone"}, "per-repo floors: gone"),
        ({"watcher_path": None}, "no PR watcher state to read"),
    ],
)
def test_floors_refuse_to_guess_when_an_input_is_unreadable(
    overrides: dict[str, Any], needle: str
) -> None:
    result = HANDLER.handle(
        ModelThroughputTickRequest.model_validate(request(**overrides))
    )
    assert any(
        line.startswith("UNKNOWN floors") and needle in line for line in result.lines
    )
    assert "floors" in result.unknown


def test_land_like_operator_logins_compare_case_insensitively() -> None:
    watcher = {
        **HEALTHY_WATCHER,
        "prs": {
            **HEALTHY_WATCHER["prs"],
            "o2": {"facts": {"repo": "r/a", "state": "OPEN", "author": "Alice "}},
        },
    }
    result = HANDLER.handle(
        ModelThroughputTickRequest.model_validate(
            request(watcher_state=watcher, land_like_operator=["alice"])
        )
    )
    assert not any("report-only" in line for line in result.lines)
    report_only = HANDLER.handle(
        ModelThroughputTickRequest.model_validate(request(watcher_state=watcher))
    )
    assert (
        "floors do not count report-only authors' PRs (landing policy): r/a 1"
        in (report_only.lines)[-1]
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "now"),
        (request(now="bad"), "now"),
        (request(launchctl="maybe"), "launchctl"),
        (request(watcher_max_minutes=-1), "greater"),
        (request(surprise=1), "surprise|Extra"),
    ],
)
def test_error_chain_refuses_before_the_handler_runs(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelThroughputTickRequest.model_validate(payload)


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_matches_a_captured_case(
    tmp_path: Path,
) -> None:
    case = next(
        c
        for c in PARITY["cases"]
        if c["expected"]["status_line"].startswith("THROUGHPUT OK")
    )
    runtime = await _run(tmp_path, case["request"])
    result = runtime.handler_result
    assert isinstance(result, ModelThroughputTickResult)
    assert result.model_dump() == case["expected"]
    assert (
        ModelThroughputTickResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_reports_a_stall(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, request(launchctl="not_loaded"))
    result = runtime.handler_result
    assert isinstance(result, ModelThroughputTickResult)
    assert result.exit_code == 1
    assert result.missing == ["controller"]


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {})
    assert runtime.handler_result is None
    (tmp_path / "state2").mkdir()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(request(launchctl="maybe")))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=bad,
        state_root=tmp_path / "state2",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
