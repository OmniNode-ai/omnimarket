# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm owner responder's contract names a pure node and routes its handler (OMN-24200)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_alarm_owner_responder_compute.models.model_alarm_owner_responder import (
    ModelAlarmOwnerPolicy,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_alarm_owner_responder_compute"
)


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load((NODE / "contract.yaml").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_contract_is_pure_and_routes_its_operation_to_a_callable_handler() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    handlers = contract["handler_routing"]["handlers"]
    assert [h["operation"] for h in handlers] == ["decide_alarm_owner"]
    for entry in handlers:
        module = importlib.import_module(entry["handler"]["module"])
        assert callable(getattr(module, entry["handler"]["name"])().handle)
        event = importlib.import_module(entry["event_model"]["module"])
        assert getattr(event, entry["event_model"]["name"])


def test_contract_topics_are_declared_and_the_terminal_event_is_published() -> None:
    contract = _contract()
    topics = contract["event_bus"]
    assert topics["subscribe_topics"] == [contract["runtime_dispatch"]["command_topic"]]
    assert topics["publish_topics"] == [contract["terminal_event"]]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.alarm-owner-responder-decided.v1"
    )


def test_the_node_is_registered_as_an_entry_point() -> None:
    pyproject = (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text(
        "utf-8"
    )
    assert (
        'node_alarm_owner_responder_compute = "omnimarket.nodes.node_alarm_owner_responder_compute"'
        in pyproject
    )


def test_the_overlay_example_loads_into_the_policy_model() -> None:
    loaded = yaml.safe_load(
        (NODE / "alarm_owner.overlay.example.yaml").read_text("utf-8")
    )
    assert isinstance(loaded, dict)
    policy = ModelAlarmOwnerPolicy.model_validate(loaded["alarm_owner_policy"])
    assert policy.alarm_actor == "launchd:lab-alarm"
    assert policy.owner_required_after_hours > 0
    assert policy.redispatch_after_hours > 0
