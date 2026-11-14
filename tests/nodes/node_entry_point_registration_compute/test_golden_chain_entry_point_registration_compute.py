# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of node_entry_point_registration_compute: the reviewed list answers for a node (OMN-24354)."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_entry_point_registration_compute import (
    HandlerEntryPointRegistration,
)
from omnimarket.nodes.node_entry_point_registration_compute.models import (
    ModelEntryPointRegistrationConfig,
    ModelEntryPointRegistrationRequest,
    ModelReviewedUnregisteredNode,
)
from omnimarket.nodes.node_entry_point_registration_compute.reviewed_list import (
    load_registration_config,
)

NODES_DIR = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"


def _config(*names: str) -> ModelEntryPointRegistrationConfig:
    return ModelEntryPointRegistrationConfig(
        reviewed_unregistered_nodes=tuple(
            ModelReviewedUnregisteredNode(node=name, reason=f"reviewed: {name}")
            for name in names
        )
    )


def test_registered_node_passes() -> None:
    verdict = HandlerEntryPointRegistration(_config()).handle(
        ModelEntryPointRegistrationRequest(
            node_name="node_a", entry_points=frozenset({"node_a"})
        )
    )
    assert (verdict.registered, verdict.reviewed_reason, verdict.passes) == (
        True,
        None,
        True,
    )


def test_reviewed_unregistered_node_passes_with_its_reason() -> None:
    verdict = HandlerEntryPointRegistration(_config("node_b")).handle(
        ModelEntryPointRegistrationRequest(node_name="node_b", entry_points=frozenset())
    )
    assert (verdict.registered, verdict.reviewed_reason, verdict.passes) == (
        False,
        "reviewed: node_b",
        True,
    )


def test_shipped_contract_list_is_unique_reasoned_and_on_disk() -> None:
    entries = load_registration_config().reviewed_unregistered_nodes
    names = [entry.node for entry in entries]
    assert names
    assert len(names) == len(set(names))
    assert all(entry.reason.strip() for entry in entries)
    assert [name for name in names if not (NODES_DIR / name).is_dir()] == []
