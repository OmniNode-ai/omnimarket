# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_fleet_partition_key_compute [OMN-14978].

Verifies contract/metadata YAML validity, handler + model imports, and the
mutual-exclusion property the whole ticket exists to prove: a deterministic,
injective repo:branch key so Kafka's same-key-same-partition guarantee keeps
one branch's stream on a single partition.
"""

from __future__ import annotations

from itertools import product
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_fleet_partition_key_compute.handlers.handler_fleet_partition_key_compute import (
    HandlerFleetPartitionKeyCompute,
)
from omnimarket.nodes.node_fleet_partition_key_compute.models import (
    ModelPartitionKeyRequest,
    derive_partition_key,
    stable_partition_index,
)

NODE_NAME = "node_fleet_partition_key_compute"


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / NODE_NAME
    )


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        assert isinstance(data, dict)
        assert data["name"] == NODE_NAME
        assert data["node_type"] == "COMPUTE_GENERIC"

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        handler = data.get("handler", {})
        assert "module" in handler
        assert "class" in handler
        routed = data["handler_routing"]["handlers"][0]["handler"]
        assert routed["name"] == handler["class"]

    def test_contract_declares_no_bus_topics(self, contract_path: Path) -> None:
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        assert "event_bus" not in data
        assert "terminal_event" not in data

    def test_contract_declares_runtime_dispatch_seam(self, contract_path: Path) -> None:
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        dispatch = data["runtime_dispatch"]
        assert dispatch["command_topic"]
        assert dispatch["terminal_events"]["success"]
        assert dispatch["terminal_events"]["failure"]

    def test_descriptor_declares_pure_compute(self, contract_path: Path) -> None:
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        descriptor = data["descriptor"]
        assert descriptor["node_archetype"] == "compute"
        assert descriptor["purity"] == "pure"


class TestMetadataYaml:
    def test_metadata_loads(self, metadata_path: Path) -> None:
        import yaml

        data = yaml.safe_load(metadata_path.read_text())
        assert data["name"] == NODE_NAME
        assert "version" in data
        assert (
            data["entry_points"]["onex.nodes"][NODE_NAME]
            == "omnimarket.nodes.node_fleet_partition_key_compute"
        )


class TestImports:
    def test_handler_imports(self) -> None:
        assert HandlerFleetPartitionKeyCompute is not None

    def test_models_import(self) -> None:
        from omnimarket.nodes.node_fleet_partition_key_compute.models import (
            ModelPartitionKeyResult,
        )

        assert ModelPartitionKeyRequest is not None
        assert ModelPartitionKeyResult is not None


class TestKeyDerivation:
    def test_key_is_repo_colon_branch(self) -> None:
        assert (
            derive_partition_key("OmniNode-ai/omnimarket", "jonah/omn-14978-test")
            == "OmniNode-ai/omnimarket:jonah/omn-14978-test"
        )

    def test_determinism_same_inputs_same_key(self) -> None:
        first = derive_partition_key("OmniNode-ai/omnimarket", "dev")
        second = derive_partition_key("OmniNode-ai/omnimarket", "dev")
        assert first == second

    def test_injectivity_distinct_pairs_distinct_keys(self) -> None:
        """The mutual-exclusion property depends on this: two DIFFERENT
        (repo, branch) pairs must NEVER collide to the same key — verified
        across a cross-product matrix, not just a couple of hand examples."""
        repos = [
            "OmniNode-ai/omnimarket",
            "OmniNode-ai/omnibase_infra",
            "OmniNode-ai/omnibase_core",
        ]
        branches = ["dev", "main", "jonah/omn-14978-a", "jonah/omn-14978-b"]
        keys = [derive_partition_key(r, b) for r, b in product(repos, branches)]
        assert len(keys) == len(set(keys))

    def test_branch_containing_colon_is_rejected(self) -> None:
        """git ref grammar forbids ':' in a branch name — the request model
        enforces this defensively so the join's injectivity guarantee never
        silently breaks on a malformed branch."""
        with pytest.raises(ValidationError):
            ModelPartitionKeyRequest(
                repo="OmniNode-ai/omnimarket",
                branch="feat:with-colon",
                partition_count=4,
            )

    def test_key_independent_of_partition_count(self) -> None:
        """The key identity does not depend on topology size — only the
        illustrative index preview does. Re-partitioning must not change
        WHICH logical stream a key names."""
        key_at_1 = derive_partition_key("OmniNode-ai/omnimarket", "dev")
        key_at_8 = derive_partition_key("OmniNode-ai/omnimarket", "dev")
        assert key_at_1 == key_at_8


class TestPartitionIndexPreview:
    def test_index_within_bounds(self) -> None:
        key = derive_partition_key("OmniNode-ai/omnimarket", "dev")
        for count in (1, 2, 4, 8, 16):
            index = stable_partition_index(key, count)
            assert 0 <= index < count

    def test_deterministic_across_calls(self) -> None:
        key = derive_partition_key("OmniNode-ai/omnimarket", "dev")
        assert stable_partition_index(key, 4) == stable_partition_index(key, 4)

    def test_single_partition_always_index_zero(self) -> None:
        key = derive_partition_key("OmniNode-ai/omnimarket", "dev")
        assert stable_partition_index(key, 1) == 0


class TestHandlerChain:
    def test_handle_round_trip(self) -> None:
        handler = HandlerFleetPartitionKeyCompute()
        request = ModelPartitionKeyRequest(
            repo="OmniNode-ai/omnimarket",
            branch="jonah/omn-14978-fleet-partition-keying",
            partition_count=4,
        )
        result = handler.handle(request)
        assert result.repo == request.repo
        assert result.branch == request.branch
        assert result.partition_key == f"{request.repo}:{request.branch}"
        assert 0 <= result.partition_index_preview < 4
        assert result.partition_count == 4

    def test_handle_determinism(self) -> None:
        handler = HandlerFleetPartitionKeyCompute()
        request = ModelPartitionKeyRequest(
            repo="OmniNode-ai/omnimarket", branch="dev", partition_count=4
        )
        assert handler.handle(request) == handler.handle(request)

    def test_partition_count_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            ModelPartitionKeyRequest(
                repo="OmniNode-ai/omnimarket", branch="dev", partition_count=0
            )
