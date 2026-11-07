# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract routes the compare and a held run reads WORKING (OMN-22959)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_workflow_baseline_compare_compute.models import (
    EnumBaselineVerdict,
    ModelWorkflowBaselineCompareReceipt,
)
from tests.nodes.node_workflow_baseline_compare_compute.support import (
    NAME,
    held,
    request,
    result,
)


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    return loaded


def _handler() -> Any:
    handler = _contract()["handler_routing"]["handlers"][0]["handler"]
    return getattr(importlib.import_module(handler["module"]), handler["name"])()


def test_golden_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()

    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == contract["event_bus"]["subscribe_topics"][0]
        == "onex.evt.omnimarket.workflow-baseline-run-observed.v1"
    )
    assert (
        contract["terminal_event"]
        == contract["event_bus"]["publish_topics"][0]
        == "onex.evt.omnimarket.workflow-baseline-compared.v1"
    )
    # A pure compute node publishes nothing itself: the cause-lane request rides
    # in the receipt, so the contract declares only the terminal topic.
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]


def test_golden_a_held_run_round_trips_through_json_as_a_working_receipt() -> None:
    wire = request(result(held())).model_dump_json()
    receipt = _handler().handle(type(request(None)).model_validate_json(wire))
    back = ModelWorkflowBaselineCompareReceipt.model_validate_json(
        receipt.model_dump_json()
    )

    assert back == receipt
    assert back.verdict is EnumBaselineVerdict.WORKING
