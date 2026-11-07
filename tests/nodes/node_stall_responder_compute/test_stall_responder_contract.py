# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stall responder's contract names a pure node and routes both handlers (OMN-22885)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_stall_responder_compute"
)


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load((NODE / "contract.yaml").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_contract_is_pure_and_routes_both_operations_to_callable_handlers() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"  # type: ignore[index]
    handlers = contract["handler_routing"]["handlers"]  # type: ignore[index]
    assert [h["operation"] for h in handlers] == [
        "decide_stall_responses",
        "sum_lab_idle_cost",
    ]
    for entry in handlers:
        module = importlib.import_module(entry["handler"]["module"])
        assert callable(getattr(module, entry["handler"]["name"])().handle)
        event = importlib.import_module(entry["event_model"]["module"])
        assert getattr(event, entry["event_model"]["name"])


def test_contract_topics_are_declared_and_the_terminal_event_is_published() -> None:
    contract = _contract()
    topics = contract["event_bus"]
    assert topics["subscribe_topics"] == [contract["runtime_dispatch"]["command_topic"]]  # type: ignore[index]
    assert topics["publish_topics"] == [contract["terminal_event"]]  # type: ignore[index]
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.stall-responder-decided.v1"
    )


def test_the_node_is_registered_as_an_entry_point() -> None:
    pyproject = (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text(
        "utf-8"
    )
    assert (
        'node_stall_responder_compute = "omnimarket.nodes.node_stall_responder_compute"'
        in pyproject
    )
