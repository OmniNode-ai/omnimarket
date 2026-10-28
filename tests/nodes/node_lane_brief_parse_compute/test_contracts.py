# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parse node takes facts only, depends on no protocol and gives the same result in process and through its contract route."""

from __future__ import annotations

import importlib
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.models.lane_brief.model_lane_brief import ModelLaneBrief
from omnimarket.models.lane_brief.model_lane_brief_issue import ModelLaneBriefIssue
from omnimarket.models.lane_brief.model_lane_brief_parse_request import (
    ModelLaneBriefParseRequest,
)
from omnimarket.models.lane_brief.model_lane_brief_parse_result import (
    ModelLaneBriefParseResult,
)
from omnimarket.nodes.node_lane_brief_gate_orchestrator.handlers.handler_lane_brief_gate_run import (
    EDGES,
    HandlerLaneBriefGateRun,
)
from omnimarket.nodes.node_lane_brief_parse_compute.handlers.handler_lane_brief_parse import (
    HandlerLaneBriefParse,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODES = REPO / "src" / "omnimarket" / "nodes"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
BRIEF = (
    "# Lane demo: Wire it\n\nLane kind: ticket\nRequired engine: codex\n"
    "Opens PR in: omnimarket\nTicket: OMN-21458\n"
)
# Values a deployment configures: a field named for one belongs in a contract overlay, not on a request.
CONFIGURED_NAMES = frozenset(
    {
        "model", "effort", "endpoint", "url", "host_url", "timeout", "timeout_ms", "threshold",
        "max_attempts", "retries", "budget", "path", "root", "token", "secret", "api_key",
        "backend", "backend_id", "lane", "topic", "bus", "ceiling",
    }
)  # fmt: skip


def _contract(node: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODES / node / "contract.yaml").read_text(encoding="utf-8")
    )
    return loaded


def test_no_field_of_the_request_or_result_models_is_a_configured_value() -> None:
    fields = {
        name
        for model in (
            ModelLaneBriefParseRequest,
            ModelLaneBriefParseResult,
            ModelLaneBrief,
            ModelLaneBriefIssue,
        )
        for name in model.model_fields
    }
    assert fields & CONFIGURED_NAMES == set()
    assert set(ModelLaneBriefParseRequest.model_fields) == {"brief", "now"}


def test_the_contract_declares_no_protocol_dependency_or_config() -> None:
    contract = _contract("node_lane_brief_parse_compute")
    assert "protocol_dependencies" not in contract
    assert "config" not in contract
    assert contract["descriptor"]["purity"] == "pure"
    assert not (NODES / "node_lane_brief_parse_compute" / "protocols").exists()


def test_the_node_is_registered_and_routes_to_the_handler() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    entries = pyproject["project"]["entry-points"]["onex.nodes"]
    for node in ("node_lane_brief_parse_compute", "node_lane_brief_gate_orchestrator"):
        assert entries[node] == f"omnimarket.nodes.{node}"
    route = _resolve_node_route("node_lane_brief_parse_compute")
    assert route.command_topic == "onex.cmd.omnimarket.lane-brief-parse-requested.v1"
    assert route.terminal_topic == "onex.evt.omnimarket.lane-brief-parsed.v1"
    assert (
        getattr(import_module_of(route.handler_module), route.handler_class)
        is HandlerLaneBriefParse
    )


def import_module_of(name: str) -> Any:
    return importlib.import_module(name)


def test_in_process_and_contract_routed_results_are_byte_identical() -> None:
    request = ModelLaneBriefParseRequest(brief=BRIEF, now=NOW)
    direct = HandlerLaneBriefParse().handle(request)
    through_gate = HandlerLaneBriefGateRun().handle(request)
    assert direct.parsed is True
    assert direct.model_dump_json() == through_gate.model_dump_json()


def test_the_gate_orchestrator_has_the_parse_node_as_its_first_edge() -> None:
    contract = _contract("node_lane_brief_gate_orchestrator")
    nodes = contract["workflow_coordination"]["workflow_definition"]["execution_graph"][
        "nodes"
    ]
    assert nodes[0]["step_config"] == {
        "node": "node_lane_brief_parse_compute",
        "operation": "parse_lane_brief",
    }
    assert EDGES[0] == ("node_lane_brief_parse_compute", "parse_lane_brief")
    assert contract["node_type"] == "orchestrator"
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.lane-brief-gate-completed.v1"
    ]
    assert contract["runtime_dispatch"]["command_topic"] == (
        "onex.cmd.omnimarket.lane-brief-gate-requested.v1"
    )
