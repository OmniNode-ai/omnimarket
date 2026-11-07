# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and the probe reaches an authorized class (OMN-19998)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_hook_drain_probe_compute.models import (
    ModelHookDrainProbeRequest,
)

NAME = "node_hook_drain_probe_compute"


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def _handler() -> Any:
    handler = _contract()["handler_routing"]["handlers"][0]["handler"]
    return getattr(importlib.import_module(handler["module"]), handler["name"])()


def test_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["side_effects"] == []
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.hook-drain-probe-requested.v1"
    ]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.hook-drain-probe-decided.v1"]
    assert contract["terminal_event"] in bus["publish_topics"]
    assert {e["operation"] for e in contract["handler_routing"]["handlers"]} == {
        "choose_drain_probe"
    }


def test_golden_chain_two_refused_classes_ahead_of_an_authorized_one() -> None:
    """The h053 shape: a prompt at the head, a content capture behind it, then the hook events."""
    result = _handler().handle(
        ModelHookDrainProbeRequest(
            pending_event_types=(
                "prompt.submitted",
                "content.captured",
                "hook.event",
                "hook.event",
                "prompt.submitted",
            )
        )
    )
    assert result.probe_order == (1, 2)
    assert result.reason == "different-classes"


def test_golden_chain_a_class_already_proved_refused_is_not_asked_again() -> None:
    result = _handler().handle(
        ModelHookDrainProbeRequest(
            pending_event_types=("prompt.submitted", "content.captured", "hook.event"),
            refused_event_types=("content.captured",),
        )
    )
    assert result.probe_order == (2,)
