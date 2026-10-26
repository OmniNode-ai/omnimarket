# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21426 AC3: the stuck-owner actor (A7) and the lease claim close node (A4) are deleted.

The landing leases move onto the generic lease orchestrator, whose TTL expiry and fence replace the
stuck-owner actor and the claim close sweep, so neither node directory, test directory nor entry point
may come back."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DELETED = ("node_stuck_owner_actor_effect", "node_landing_lease_claim_close_effect")


@pytest.mark.unit
@pytest.mark.parametrize("node", DELETED)
def test_node_directory_is_deleted(node: str) -> None:
    assert not (REPO / "src" / "omnimarket" / "nodes" / node).exists()
    assert not (REPO / "tests" / "nodes" / node).exists()


@pytest.mark.unit
@pytest.mark.parametrize("node", DELETED)
def test_node_entry_point_is_removed(node: str) -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert node not in project["project"]["entry-points"]["onex.nodes"]


@pytest.mark.unit
def test_cause_claim_ownership_reads_acting_owners_itself() -> None:
    from omnimarket.nodes.node_cause_claim_ownership_effect import handler

    assert callable(handler.acting)
    assert "node_stuck_owner_actor_effect" not in Path(handler.__file__).read_text(
        encoding="utf-8"
    )
