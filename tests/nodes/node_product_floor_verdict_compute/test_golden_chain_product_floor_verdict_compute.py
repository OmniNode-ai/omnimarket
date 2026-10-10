# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18008: captured floor-alarm parity, registered contract and in-memory bus proof.

The fixture comes from executing the unchanged floor-alarm script's compute() with its
reads replaced by synthetic caller facts. No legacy source is needed to run these tests. Missing scans and configurable silence are new caller
boundary behavior tested separately from the captured decisions.
"""

from __future__ import annotations

import copy
import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_product_floor_verdict_compute as node_package
from omnimarket.nodes.node_product_floor_verdict_compute.handlers.handler_product_floor_verdict import (
    HandlerProductFloorVerdict,
)
from omnimarket.nodes.node_product_floor_verdict_compute.models.model_product_floor_verdict import (
    ModelProductFloorVerdictRequest,
    ModelProductFloorVerdictResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.product-floor-verdict-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.product-floor-verdict-completed.v1"
PARITY = json.loads(
    (ROOT / "tests/fixtures/product_floor_verdict_parity.json").read_text()
)
HANDLER = HandlerProductFloorVerdict()


def decide(payload: dict[str, Any]) -> ModelProductFloorVerdictResult:
    return HANDLER.handle(ModelProductFloorVerdictRequest.model_validate(payload))


def as_old(result: ModelProductFloorVerdictResult) -> dict[str, Any]:
    return result.model_dump(mode="json", exclude={"verdict"})


@pytest.mark.parametrize("case", PARITY, ids=lambda c: str(c["name"]))
def test_old_behavior_parity(case: dict[str, Any]) -> None:
    result = decide(case["request"])
    assert as_old(result) == case["expected"]
    expected = case["expected"]
    verdict = (
        "BREACH"
        if expected["breaches"]
        else "UNKNOWN"
        if expected["unknowns"]
        else "OK"
    )
    assert result.verdict == verdict


def test_ts_and_controller_cell_are_the_captured_stamp_and_cell() -> None:
    for case in PARITY:
        result = decide(case["request"])
        assert result.ts == case["expected"]["ts"]
        assert result.controller_cell == case["expected"]["controller_cell"]


def test_fixture_exercises_every_breach_and_unknown_key_family() -> None:
    assert len(PARITY) >= 16

    def families(key: str) -> str:
        return key.split(":", 1)[0]

    breaches = {families(k) for c in PARITY for k in c["expected"]["breaches"]}
    unknowns = {families(k) for c in PARITY for k in c["expected"]["unknowns"]}
    assert breaches == {"floor", "controller"}
    assert unknowns == {
        "floors",
        "waiting",
        "clone-sync",
        "clone",
        "floor",
        "controller",
    }
    text = "\n".join(line for c in PARITY for line in c["expected"]["summary_lines"])
    for needle in (
        "FLOOR-ALARM OK",
        "FLOOR-ALARM BREACH",
        "FLOOR-ALARM UNKNOWN",
        "idle:",
        "UNKNOWN waiting",
        "watcher state is stale",
        "'prs'",
        "clone-sync absent or stale",
        "cannot read clone-sync:",
        "no clone for",
        "cannot count",
        "positive control failed",
        "none in 7 days",
        "controller silent for",
        "only 1 of 3 controller ticks available",
        "Expecting value",
        "invalid worker count",
        "no controller ticks",
    ):
        assert needle in text, needle
    encoded = json.dumps(PARITY)
    assert "/home/" not in encoded
    assert any(c["expected"]["notes"] for c in PARITY)
    assert any(
        f["docs_only"] for c in PARITY for f in c["expected"]["per_repo"].values()
    )


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["externally_consumed_topics"] == [TERMINAL_TOPIC]
    assert (
        contract["handler_routing"]["handlers"][0]["operation"]
        == "decide_product_floor_verdict"
    )
    assert contract["metadata"]["related_tickets"] == ["OMN-18008"]
    assert contract["metadata"]["transport_type"] == "inmemory"
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeProductFloorVerdictCompute, handler_type)
    for side, model in (
        ("input_model", ModelProductFloorVerdictRequest),
        ("output_model", ModelProductFloorVerdictResult),
    ):
        assert (
            getattr(
                importlib.import_module(contract[side]["module"]),
                contract[side]["name"],
            )
            is model
        )
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_product_floor_verdict_compute"].load() is node_package


def test_missing_scan_is_an_explicit_unknown() -> None:
    payload = copy.deepcopy(PARITY[0]["request"])
    payload["scans"] = {}
    result = decide(payload)
    assert result.unknowns == {
        "floor:omnibase_core": "cannot count omnibase_core: no scan supplied"
    }
    assert result.verdict == "UNKNOWN"
    assert result.per_repo["omnibase_core"].count is None


def test_lane_ticket_and_silent_threshold_come_from_caller() -> None:
    payload = copy.deepcopy(PARITY[0]["request"])
    payload.update(
        row_lane="synthetic-lane",
        row_ticket="OMN-18008",
        controller_silent_after_minutes=0.5,
    )
    result = decide(payload)
    assert " | lane=synthetic-lane | ticket=OMN-18008 | " in result.status_row
    assert result.breaches["controller"].startswith("controller silent for 1 min;")
    payload["controller_silent_after_minutes"] = 1
    assert "controller" not in decide(payload).breaches


def test_handler_is_repeatable_and_does_not_mutate_caller_facts() -> None:
    payload = copy.deepcopy(PARITY[0]["request"])
    before = copy.deepcopy(payload)
    request = ModelProductFloorVerdictRequest.model_validate(payload)
    assert HANDLER.handle(request) == HANDLER.handle(request)
    assert payload == before
    assert (
        request.model_dump()
        == ModelProductFloorVerdictRequest.model_validate(before).model_dump()
    )
    with pytest.raises(ValidationError, match="frozen"):
        request.now = "2026-10-09T13:00:00Z"


@pytest.mark.parametrize(
    "changes",
    [
        {"floors": None, "floors_unknown": None},
        {"floors_unknown": "unread"},
        {"window_hours": 0},
        {"window_hours": -1},
        {"window_hours": float("inf")},
        {"window_hours": float("nan")},
        {"max_input_age_minutes": 0},
        {"max_input_age_minutes": float("inf")},
        {"max_input_age_minutes": float("nan")},
        {"controller_ticks": 0},
        {"controller_ticks": -1},
        {"controller_ticks": float("inf")},
        {"controller_ticks": float("nan")},
        {"controller_ticks": 1.5},
        {"surprise": 1},
        {"now": "invalid"},
    ],
)
def test_error_chain_refuses_malformed_requests(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelProductFloorVerdictRequest.model_validate(
            {**PARITY[0]["request"], **changes}
        )


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
@pytest.mark.parametrize(
    "name", ["all-green", "under-floor-waiting", "controller-fewer-rows"]
)
async def test_golden_chain_over_bus_matches_captured_case(
    tmp_path: Path, name: str
) -> None:
    case = next(c for c in PARITY if c["name"] == name)
    runtime = await _run(tmp_path, case["request"])
    result = runtime.handler_result
    assert isinstance(result, ModelProductFloorVerdictResult)
    assert as_old(result) == case["expected"]
    assert (
        ModelProductFloorVerdictResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_bus_fails_without_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {})
    assert runtime.handler_result is None
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**PARITY[0]["request"], "window_hours": 0}))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=bad,
        state_root=tmp_path / "state2",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
