# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract and golden chain for compaction latency qualification compute (OMN-21114)."""

from __future__ import annotations

import importlib
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.nodes.node_context_compaction_latency_compute.handlers import (
    HandlerLatencyQualification,
)
from omnimarket.nodes.node_context_compaction_latency_compute.models import (
    ModelLatencyQualificationRequest,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
NODE_NAME = "node_context_compaction_latency_compute"
PACKAGE = f"omnimarket.nodes.{NODE_NAME}"
NODE = ROOT / "src/omnimarket/nodes" / NODE_NAME
COMMAND_TOPIC = "onex.cmd.omniintelligence.context-compaction-latency-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.context-compaction-latency-completed.v1"


def test_golden_chain_one_sample_table_yields_proposals() -> None:
    """Two hosts and three sizes of 20 cold and 20 warm trips yield a complete table."""
    trips = [
        {
            "round_trip_id": f"{size}-{host}-{temperature}-{i}",
            "size": size,
            "host": host,
            "temperature": temperature,
            "deterministic_build_ms": 100 + i,
            "model_section_ms": 1000 + i,
        }
        for size in ("smallest", "median", "largest")
        for host in ("launching-mac", "lab-host")
        for temperature in ("cold", "warm")
        for i in range(1, 21)
    ]
    result = HandlerLatencyQualification().handle(
        ModelLatencyQualificationRequest.model_validate(
            {
                "round_trips": trips,
                "required_sizes": ["smallest", "median", "largest"],
                "required_hosts": ["launching-mac", "lab-host"],
                "margin_ms": 1000,
            }
        )
    )
    assert result.complete
    assert len(result.cells) == 12
    # per trip total = 1100 + 2i; the 19th of 20 sorted totals is 1138
    assert result.build_p95_ms == 1138
    assert result.class_ceiling_ms == result.build_p95_ms + 1000
    assert result.precompute_deadline_ms == result.build_p95_ms + 5000


def test_contract_loads_and_declares_its_topics() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["name"] == "context_compaction_latency_compute"
    assert contract["node_type"] == "compute"
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [COMPLETED_TOPIC]
    assert contract["terminal_event"] == COMPLETED_TOPIC
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["externally_consumed_topics"] == [COMPLETED_TOPIC]
    assert contract["metadata"] == {"ticket": "OMN-21114", "transport_type": "kafka"}
    assert contract["descriptor"] == {
        "node_archetype": "compute",
        "purity": "pure",
        "idempotent": True,
        "side_effects": [],
        "runtime_profiles": ["main"],
    }
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert [route["operation"] for route in routing["handlers"]] == ["qualify_latency"]


def test_contract_handler_and_models_resolve() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    route = contract["handler_routing"]["handlers"][0]
    declaration = route["handler"]
    assert declaration["name"] == "HandlerLatencyQualification"
    assert declaration["module"].startswith(f"{PACKAGE}.handlers.")
    handler_class = getattr(
        importlib.import_module(declaration["module"]), declaration["name"]
    )
    assert handler_class.__module__ == declaration["module"]
    assert callable(handler_class().handle)
    for key, expected in (
        ("input_model", "ModelLatencyQualificationRequest"),
        ("output_model", "ModelLatencyQualificationResult"),
    ):
        model_path = route[key]
        assert model_path == f"{PACKAGE}.models.model_latency_qualification.{expected}"
        module_name, class_name = model_path.rsplit(".", 1)
        assert issubclass(
            getattr(importlib.import_module(module_name), class_name), BaseModel
        )


def test_entry_point_registered_and_loads() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    metadata = yaml.safe_load((NODE / "metadata.yaml").read_text(encoding="utf-8"))
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert project["project"]["entry-points"]["onex.nodes"][NODE_NAME] == PACKAGE
    assert metadata["entry_points"]["onex.nodes"][NODE_NAME] == PACKAGE
    assert "entry_points" not in contract
    registered = {entry.name: entry for entry in entry_points(group="onex.nodes")}
    assert registered[NODE_NAME].value == PACKAGE
    assert registered[NODE_NAME].load() is importlib.import_module(PACKAGE)
    assert metadata["pack"] == "core"
    assert metadata["node_role"] == "compute"
    assert metadata["capabilities"]["side_effect_class"] == "none"
