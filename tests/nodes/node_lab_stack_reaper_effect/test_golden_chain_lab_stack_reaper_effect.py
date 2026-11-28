# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the packaged reaper contract, then lease and reap through its declared handler."""

from __future__ import annotations

import importlib
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_stack_reaper_effect.handlers.handler_lab_stack_reaper import (
    STANDING_STACKS_SCHEMA,
)
from omnimarket.nodes.node_lab_stack_reaper_effect.models import (
    ModelLabStackReaperRequest,
)

pytestmark = pytest.mark.unit

NAME = "node_lab_stack_reaper_effect"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    assert isinstance(loaded, dict)
    return loaded


def test_golden_chain_lab_stack_reaper_effect(tmp_path: Path) -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "effect"
    routing = contract["handler_routing"]
    assert isinstance(routing, dict)
    assert {entry["operation"] for entry in routing["handlers"]} == {
        "run_lab_stack_reaper"
    }
    entry = routing["handlers"][0]
    handler_type = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    request_type = _resolve(entry["input_model"])
    assert request_type is ModelLabStackReaperRequest
    _resolve(entry["output_model"])
    census = tmp_path / "census.yaml"
    census.write_text(
        "lanes:\n  dev:\n    compose_project: protected-dev\n", encoding="utf-8"
    )
    leased = handler_type().handle(
        request_type(
            "lease",
            lease_dir=tmp_path / "leases",
            census=census,
            project="lab-golden-test",
            lane="worker",
            claim="2026-10-15T10:00:00Z",
            generation="one",
        )
    )
    assert leased.decision == "leased"
    assert leased.ok
    refused = handler_type().handle(
        request_type(
            "lease",
            lease_dir=tmp_path / "leases",
            census=census,
            project="protected-dev",
        )
    )
    assert refused.decision == "refused"


def test_the_contract_carries_no_bus_route_and_hardcodes_no_topic() -> None:
    contract = _contract()
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert "terminal_event" not in contract
    source = files(f"omnimarket.nodes.{NAME}.handlers")
    for handler in ("handler_lab_stack_reaper.py", "lab_stack_reaper_cli.py"):
        assert "onex.evt." not in source.joinpath(handler).read_text()
        assert "onex.cmd." not in source.joinpath(handler).read_text()


def test_the_packaged_standing_stack_example_has_the_declared_shape() -> None:
    packaged = files(f"omnimarket.nodes.{NAME}").joinpath(
        "standing_stacks.example.yaml"
    )
    declaration = yaml.safe_load(packaged.read_text())
    assert declaration["schema"] == STANDING_STACKS_SCHEMA
    assert all(isinstance(item["project"], str) for item in declaration["projects"])


def _resolve(dotted: str) -> type:
    module, _, attribute = dotted.rpartition(".")
    resolved = getattr(importlib.import_module(module), attribute)
    assert isinstance(resolved, type)
    return resolved
