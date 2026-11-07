# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract, golden chain and error chain for additive latency qualification compute (OMN-21134)."""

from __future__ import annotations

import importlib
import json
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from omnimarket.models.claude_code_binding import ADDITIVE_BUDGET_CAPS_MS
from omnimarket.nodes.node_context_additive_latency_compute.handlers import (
    HandlerAdditiveLatency,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
NODE_NAME = "node_context_additive_latency_compute"
PACKAGE = f"omnimarket.nodes.{NODE_NAME}"
NODE = ROOT / "src/omnimarket/nodes" / NODE_NAME
COMMAND_TOPIC = "onex.cmd.omniintelligence.context-additive-latency-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.context-additive-latency-completed.v1"
HOSTS = ["launching-mac", "lab-host"]


def _contract() -> dict[str, object]:
    return yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))


def _route_models() -> tuple[type[BaseModel], type[BaseModel]]:
    route = _contract()["handler_routing"]["handlers"][0]  # type: ignore[index]
    resolved: list[type[BaseModel]] = []
    for key in ("input_model", "output_model"):
        module_name, class_name = route[key].rsplit(".", 1)
        resolved.append(getattr(importlib.import_module(module_name), class_name))
    return resolved[0], resolved[1]


def _command_payload(*, drop_last_warm_trip: bool = False) -> str:
    """The JSON a runner publishes on the command topic: 20 cold and 20 warm trips per cell."""
    trips = [
        {
            "round_trip_id": f"{profile}-{host}-{temperature}-{i}",
            "profile": profile,
            "host": host,
            "temperature": temperature,
            "duration_ms": cap / 100 * i,
        }
        for profile, cap in ADDITIVE_BUDGET_CAPS_MS.items()
        for host in HOSTS
        for temperature in ("cold", "warm")
        for i in range(1, 21)
    ]
    if drop_last_warm_trip:
        trips = trips[:-1]
    return json.dumps(
        {
            "round_trips": trips,
            "required_profiles": list(ADDITIVE_BUDGET_CAPS_MS),
            "required_hosts": HOSTS,
        }
    )


def test_golden_chain_complete_table_yields_one_budget_per_profile() -> None:
    """Command in, completed event out: every profile gets a budget inside its cap."""
    input_model, output_model = _route_models()
    request = input_model.model_validate_json(_command_payload())
    event = output_model.model_validate(
        HandlerAdditiveLatency().handle(request).model_dump(mode="json")
    )
    body = event.model_dump(mode="json")
    assert body["complete"] is True
    assert body["falsifiers"] == []
    assert len(body["cells"]) == len(ADDITIVE_BUDGET_CAPS_MS) * len(HOSTS) * 2
    budgets = {p["profile"]: p["proposed_budget_ms"] for p in body["proposals"]}
    # trip i of 20 lasts cap/100 * i, so the nearest-rank p95 is the 19th: 0.19 of the cap
    assert budgets == {
        profile: int(cap / 100 * 19) for profile, cap in ADDITIVE_BUDGET_CAPS_MS.items()
    }


def test_error_chain_a_short_table_withholds_every_budget_in_the_event() -> None:
    """One missing warm round trip: the completed event carries no budget and names the cell."""
    input_model, output_model = _route_models()
    request = input_model.model_validate_json(
        _command_payload(drop_last_warm_trip=True)
    )
    body = output_model.model_validate(
        HandlerAdditiveLatency().handle(request).model_dump(mode="json")
    ).model_dump(mode="json")
    assert body["complete"] is False
    assert all(p["proposed_budget_ms"] is None for p in body["proposals"])
    assert any("round trips" in f for f in body["falsifiers"])


def test_error_chain_a_command_naming_no_additive_profile_is_refused() -> None:
    """A trip for a profile with no cap never reaches the handler."""
    input_model, _ = _route_models()
    payload = json.loads(_command_payload())
    payload["round_trips"][0]["profile"] = "compaction_orchestrator"
    with pytest.raises(ValidationError, match="additive profile"):
        input_model.model_validate(payload)


def test_contract_loads_and_declares_its_topics() -> None:
    contract = _contract()
    assert contract["name"] == "context_additive_latency_compute"
    assert contract["node_type"] == "compute"
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]  # type: ignore[index]
    assert contract["event_bus"]["publish_topics"] == [COMPLETED_TOPIC]  # type: ignore[index]
    assert contract["terminal_event"] == COMPLETED_TOPIC
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC  # type: ignore[index]
    assert contract["externally_consumed_topics"] == [COMPLETED_TOPIC]
    assert contract["metadata"] == {"ticket": "OMN-21134", "transport_type": "kafka"}
    assert contract["descriptor"] == {
        "node_archetype": "compute",
        "purity": "pure",
        "idempotent": True,
        "side_effects": [],
        "runtime_profiles": ["main"],
    }
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"  # type: ignore[index]
    assert [r["operation"] for r in routing["handlers"]] == [  # type: ignore[index]
        "qualify_additive_latency"
    ]


def test_contract_handler_and_models_resolve() -> None:
    route = _contract()["handler_routing"]["handlers"][0]  # type: ignore[index]
    declaration = route["handler"]
    assert declaration["name"] == "HandlerAdditiveLatency"
    assert declaration["module"].startswith(f"{PACKAGE}.handlers.")
    handler_class = getattr(
        importlib.import_module(declaration["module"]), declaration["name"]
    )
    assert handler_class.__module__ == declaration["module"]
    assert callable(handler_class().handle)
    input_model, output_model = _route_models()
    assert input_model.__name__ == "ModelAdditiveLatencyRequest"
    assert output_model.__name__ == "ModelAdditiveLatencyResult"


def test_entry_point_registered_and_loads() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    metadata = yaml.safe_load((NODE / "metadata.yaml").read_text(encoding="utf-8"))
    assert project["project"]["entry-points"]["onex.nodes"][NODE_NAME] == PACKAGE
    assert metadata["entry_points"]["onex.nodes"][NODE_NAME] == PACKAGE
    registered = {entry.name: entry for entry in entry_points(group="onex.nodes")}
    assert registered[NODE_NAME].value == PACKAGE
    assert registered[NODE_NAME].load() is importlib.import_module(PACKAGE)
    assert metadata["pack"] == "core"
    assert metadata["node_role"] == "compute"
    assert metadata["capabilities"]["side_effect_class"] == "none"
