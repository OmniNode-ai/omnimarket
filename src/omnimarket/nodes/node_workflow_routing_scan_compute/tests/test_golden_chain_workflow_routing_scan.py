# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract routes the scan and a fully read, fully hosted tree scans to zero (OMN-22948)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_workflow_routing_scan_compute.models import (
    EnumRoutingResolution,
    EnumRoutingSelection,
    ModelRoutingRoute,
)
from omnimarket.nodes.node_workflow_routing_scan_compute.tests.conftest_support import (
    EXPECTED,
    NAME,
    PLANTED_WORKFLOW,
    all_hosted_repositories,
    repository,
    request,
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
        == "onex.cmd.omnimarket.workflow-routing-scan-requested.v1"
    )
    assert (
        contract["terminal_event"]
        == contract["event_bus"]["publish_topics"][0]
        == "onex.evt.omnimarket.workflow-routing-scanned.v1"
    )


def test_golden_fixture_names_twenty_four_distinct_repositories() -> None:
    names = EXPECTED

    assert len(names) == 24
    assert len(set(names)) == 24


def test_golden_all_repositories_read_and_hosted_scan_to_zero_unresolved() -> None:
    result = _handler().handle(request(all_hosted_repositories()))

    assert result.complete is True
    assert result.repositories_read == 24
    assert result.unresolved_count == 0
    assert {scenario.resolution for scenario in result.scenarios} == {
        EnumRoutingResolution.HOSTED
    }
    assert {scenario.selection for scenario in result.scenarios} == {
        EnumRoutingSelection.HOSTED_LABEL
    }


def test_golden_a_declared_route_resolves_a_lab_job_and_is_counted_as_pending() -> None:
    names = EXPECTED
    repositories = (
        repository(names[0], ("lab.yml", PLANTED_WORKFLOW)),
        *all_hosted_repositories()[1:],
    )
    route = ModelRoutingRoute(
        repository=names[0],
        workflow=".github/workflows/lab.yml",
        job="lab-only",
        kind="lab_job_node",
    )

    result = _handler().handle(request(repositories, routes=(route,)))
    lab = [s for s in result.scenarios if s.job == "lab-only"]

    assert result.unresolved_count == 0
    assert result.pending_execution_count == len(lab) == 2
    assert {s.resolution for s in lab} == {EnumRoutingResolution.MOVED_TO_NODE}
