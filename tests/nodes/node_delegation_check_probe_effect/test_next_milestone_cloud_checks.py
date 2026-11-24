# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cloud variants of the ten delegation checks are listed as checks for the next milestone (OMN-26642).

The cloud workflows belong to the cloud owner's repository and stay as they are; this repository only lists them. A contract
test fails when one of the ten checks has no cloud variant on the list, when the list names an action other than keeping the
workflow, or when a default route of the build checks node dispatches a workflow.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    DEFAULT_CHECK_ROUTES,
    DELEGATION_CHECKS,
)

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
CONTRACT = NODES / "node_delegation_check_probe_effect/contract.yaml"
BUILD_EXAMPLE = NODES / "node_build_delegation_checks/overlay.example.yaml"


def _next_milestone() -> dict[str, object]:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["next_milestone"]


@pytest.mark.unit
def test_every_one_of_the_ten_checks_has_its_cloud_variant_on_the_next_milestone_list() -> (
    None
):
    entries = _next_milestone()["cloud_checks"]
    assert isinstance(entries, list)
    listed = sorted(e["check"] for e in entries)
    assert listed == sorted(DELEGATION_CHECKS)


@pytest.mark.unit
def test_each_entry_names_the_workflow_and_the_repository_that_owns_it() -> None:
    for entry in _next_milestone()["cloud_checks"]:  # type: ignore[union-attr]
        assert entry["workflow"].endswith(".yml"), entry
        assert "repository" not in entry, entry
        assert entry["owner"] == "cloud-owner", entry
        assert entry["disposition"] == "keep", entry
        assert entry["gateway_graded_by"] == "node_delegation_check_probe_effect", entry


@pytest.mark.unit
def test_the_list_is_the_next_milestone_and_deletes_nothing() -> None:
    milestone = _next_milestone()
    assert milestone["milestone"] == "next"
    text = CONTRACT.read_text(encoding="utf-8").lower()
    assert "delete" not in str(milestone).lower()
    assert "disposition: remove" not in text


@pytest.mark.unit
def test_a_missing_cloud_check_would_fail_the_list_test() -> None:
    entries = [e for e in _next_milestone()["cloud_checks"] if e["check"] != "C28"]  # type: ignore[union-attr]
    assert sorted(e["check"] for e in entries) != sorted(DELEGATION_CHECKS)


@pytest.mark.unit
def test_no_default_route_of_the_build_node_dispatches_a_cloud_workflow() -> None:
    assert set(DEFAULT_CHECK_ROUTES) == set(DELEGATION_CHECKS)
    for route in DEFAULT_CHECK_ROUTES.values():
        joined = " ".join(route).lower()
        assert "workflow" not in joined
        assert "gh " not in joined
        assert "node_delegation_check_probe_effect" in joined
    example = yaml.safe_load(BUILD_EXAMPLE.read_text(encoding="utf-8"))
    assert set(example["check_routes"]) == set(DELEGATION_CHECKS)
