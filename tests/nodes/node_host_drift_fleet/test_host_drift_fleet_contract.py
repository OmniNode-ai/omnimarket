# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract, package registration and deployment-neutral source checks."""

import importlib
import re
import tomllib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from omnimarket.nodes.node_host_drift_fleet import NodeHostDriftFleet
from omnimarket.nodes.node_host_drift_fleet.models import ModelHostDriftFleetOverlay
from omnimarket.nodes.node_host_drift_fleet.node import load_topics

pytestmark = pytest.mark.unit
PACKAGE = NodeHostDriftFleet.contract_path.parent


def test_host_drift_fleet_contract_bindings() -> None:
    contract = yaml.safe_load((PACKAGE / "contract.yaml").read_text())
    metadata = yaml.safe_load((PACKAGE / "metadata.yaml").read_text())
    assert contract["name"] == "node_host_drift_fleet"
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert contract["descriptor"]["node_archetype"] == "effect"
    assert contract["descriptor"]["purity"] == "effectful"
    assert contract["descriptor"]["timeout_ms"] > 0
    assert contract["metadata"]["related_tickets"]
    assert contract["event_bus"] == {
        "subscribe_topics": [
            "onex.evt.omnimarket.host-drift-heartbeat.v1",
            "onex.evt.omnimarket.host-drift-resolved.v1",
        ],
        "publish_topics": [],
    }
    assert load_topics() == tuple(contract["event_bus"]["subscribe_topics"])
    assert contract["caller_subscription"]["mode"] == "stable_group_per_host"
    assert contract["caller_subscription"]["group_id_template"] == "{prefix}.{host}"
    assert metadata["capabilities"]["requires_network"] is True
    assert metadata["capabilities"]["side_effect_class"] == "read_only"
    assert metadata["pack"] == "distributed_validation"
    binding = contract["handler"]
    handler = getattr(importlib.import_module(binding["module"]), binding["class"])
    hints = get_type_hints(handler.handle)
    for key, hint in (("input_model", "request"), ("output_model", "return")):
        module, _, name = binding[key].rpartition(".")
        model = getattr(importlib.import_module(module), name)
        assert issubclass(model, BaseModel)
        assert model.model_config == {"frozen": True, "extra": "forbid"}
        assert hints[hint] is model
        declaration = contract[key]
        assert (
            getattr(importlib.import_module(declaration["module"]), declaration["name"])
            is model
        )
    project = Path(__file__).resolve().parents[3] / "pyproject.toml"
    entry_points = tomllib.loads(project.read_text())["project"]["entry-points"][
        "onex.nodes"
    ]
    assert (
        entry_points["node_host_drift_fleet"]
        == "omnimarket.nodes.node_host_drift_fleet"
    )


def test_host_drift_fleet_contract_overlay_example() -> None:
    example = ModelHostDriftFleetOverlay.model_validate(
        yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    )
    assert example.hosts == ("host-a", "host-b")
    assert example.consumer_group_prefix
    for field, value in (
        ("heartbeat_stale_seconds", 0),
        ("read_window_seconds", 0),
        ("consumer_group_prefix", " "),
        ("unknown", True),
    ):
        with pytest.raises(ValidationError):
            ModelHostDriftFleetOverlay.model_validate(
                {**example.model_dump(), field: value}
            )


def test_host_drift_fleet_contract_source_scan() -> None:
    denied = (
        r"onex\.evt",
        r"onex\.cmd",
        r"\buuid\w*\b",
        r"\bh\d{3}\b",
        "/" + "Users" + "/",
        "omnibase_" + "internal",
    )
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text()
        for pattern in denied:
            assert re.search(pattern, source) is None, (path.name, pattern)
