# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21753 (under OMN-21288): every folded writer names its retired consumer group.

The one-runtime fold moves the five in-process projection writers from their
own containers into the runtime. The runtime's consumer group for a writer is
new, and a new group starts at the bus default (latest), so events published
between the container stopping and the runtime joining were never projected.
Each writer contract declares the container's group under
``consumer_group_handover``; omnibase_infra's subscribe preflight copies that
group's committed offsets into the runtime's group before it joins.

The container's group is ``{env}.omnimarket-projections.<writer>-writer.consume.v1``
(the compose files set it as KAFKA_CONSUMER_GROUP). A writer folded with no
declaration, or one naming another writer's group, would hand over the wrong
position or none, so both are refused here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"

# Contract directory -> the retired container's node segment.
FOLDED_WRITERS = {
    "node_projection_live_events": "live-events-writer",
    "node_projection_registration": "registration-writer",
    "node_projection_savings": "savings-writer",
    "node_projection_tenant_credentials": "tenant-credentials-writer",
    "node_projection_tenant_registry": "tenant-registry-writer",
}


@pytest.mark.parametrize(("node_dir", "retired_node"), sorted(FOLDED_WRITERS.items()))
def test_folded_writer_declares_its_retired_consumer_group(
    node_dir: str, retired_node: str
) -> None:
    contract = yaml.safe_load((NODES / node_dir / "contract.yaml").read_text())

    assert contract["consumer_group_handover"] == {
        "from_service": "omnimarket-projections",
        "from_node": retired_node,
        "from_version": "v1",
        "from_auto_offset_reset": "earliest",
    }


def _runtime_profiles(contract: dict[str, object]) -> list[str]:
    """Top-level ``runtime_profiles`` or the descriptor's; contracts use both."""
    descriptor = contract.get("descriptor")
    nested = (
        descriptor.get("runtime_profiles") if isinstance(descriptor, dict) else None
    )
    declared = contract.get("runtime_profiles") or nested or []
    return [str(profile) for profile in declared] if isinstance(declared, list) else []


@pytest.mark.parametrize("node_dir", sorted(FOLDED_WRITERS))
def test_each_listed_writer_is_one_the_runtime_can_own(node_dir: str) -> None:
    """Each listed contract carries its projection-writer runtime profile."""
    contract = yaml.safe_load((NODES / node_dir / "contract.yaml").read_text())
    profile = "projection-writer-" + node_dir.removeprefix("node_projection_").replace(
        "_", "-"
    )
    assert profile in _runtime_profiles(contract)
