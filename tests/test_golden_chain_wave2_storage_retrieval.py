# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for OMN-8298 Wave 2 storage/retrieval effect nodes.

Verifies the 5 migrated nodes (memory_storage, memory_retrieval, persona_storage,
persona_retrieval, agent_learning_retrieval) expose working contracts, models,
and entry points from the omnimarket.nodes.* namespace. Adapters stay in
omnimemory and are wired via DI at runtime, so these tests focus on the
contract/model surface that omnimarket now owns.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
import yaml

NODE_NAMES = [
    "node_memory_storage_effect",
    "node_memory_retrieval_effect",
    "node_persona_storage_effect",
    "node_persona_retrieval_effect",
    "node_agent_learning_retrieval_effect",
]


@pytest.mark.unit
@pytest.mark.parametrize("node_name", NODE_NAMES)
def test_node_contract_yaml_is_present_and_parses(node_name: str) -> None:
    """Every migrated node must ship a parseable contract.yaml."""
    contract_path = Path(
        resources.files("omnimarket.nodes") / node_name / "contract.yaml"  # type: ignore[arg-type]
    )
    assert contract_path.exists(), f"{node_name}/contract.yaml missing"
    data = yaml.safe_load(contract_path.read_text())
    assert data["name"] == node_name
    assert data["node_type"] == "EFFECT"


@pytest.mark.unit
@pytest.mark.parametrize("node_name", NODE_NAMES)
def test_node_contract_schema_refs_point_at_omnimarket(node_name: str) -> None:
    """After migration, schema_ref entries must use the omnimarket namespace."""
    contract_path = Path(
        resources.files("omnimarket.nodes") / node_name / "contract.yaml"  # type: ignore[arg-type]
    )
    text = contract_path.read_text()
    assert 'schema_ref: "omnimemory.nodes' not in text, (
        f"{node_name} still has stale omnimemory schema_ref"
    )


@pytest.mark.unit
class TestMemoryStorageNode:
    """node_memory_storage_effect — contract + models importable."""

    def test_models_importable(self) -> None:
        from omnimarket.nodes.node_memory_storage_effect import (
            ModelMemoryStorageRequest,
            ModelMemoryStorageResponse,
        )

        assert ModelMemoryStorageRequest is not None
        assert ModelMemoryStorageResponse is not None

    def test_list_operation_request_instantiates(self) -> None:
        from omnimarket.nodes.node_memory_storage_effect import (
            ModelMemoryStorageRequest,
        )

        request = ModelMemoryStorageRequest(operation="list")
        assert request.operation == "list"


@pytest.mark.unit
class TestMemoryRetrievalNode:
    """node_memory_retrieval_effect — contract + models importable."""

    def test_models_importable(self) -> None:
        from omnimarket.nodes.node_memory_retrieval_effect import (
            ModelHandlerMemoryRetrievalConfig,
            ModelMemoryRetrievalRequest,
            ModelMemoryRetrievalResponse,
            ModelSearchResult,
        )

        assert ModelHandlerMemoryRetrievalConfig is not None
        assert ModelMemoryRetrievalRequest is not None
        assert ModelMemoryRetrievalResponse is not None
        assert ModelSearchResult is not None

    def test_search_request_instantiates(self) -> None:
        from omnimarket.nodes.node_memory_retrieval_effect import (
            ModelMemoryRetrievalRequest,
        )

        request = ModelMemoryRetrievalRequest(
            operation="search",
            query_text="example",
            limit=5,
            similarity_threshold=0.7,
        )
        assert request.operation == "search"
        assert request.query_text == "example"


@pytest.mark.unit
class TestPersonaStorageNode:
    """node_persona_storage_effect — contract + models importable."""

    def test_models_importable(self) -> None:
        from omnimarket.nodes.node_persona_storage_effect import (
            ModelPersonaStorageRequest,
            ModelPersonaStorageResponse,
        )

        assert ModelPersonaStorageRequest is not None
        assert ModelPersonaStorageResponse is not None


@pytest.mark.unit
class TestPersonaRetrievalNode:
    """node_persona_retrieval_effect — contract + models importable."""

    def test_models_importable(self) -> None:
        from omnimarket.nodes.node_persona_retrieval_effect import (
            ModelPersonaRetrievalRequest,
            ModelPersonaRetrievalResponse,
        )

        assert ModelPersonaRetrievalRequest is not None
        assert ModelPersonaRetrievalResponse is not None


@pytest.mark.unit
class TestAgentLearningRetrievalNode:
    """node_agent_learning_retrieval_effect — contract + models importable."""

    def test_models_importable(self) -> None:
        from omnimarket.nodes.node_agent_learning_retrieval_effect import (
            EnumRetrievalMatchType,
            EnumRetrievalTaskType,
            ModelAgentLearningRetrievalRequest,
            ModelAgentLearningRetrievalResponse,
            ModelRetrievedLearning,
        )

        assert EnumRetrievalMatchType is not None
        assert EnumRetrievalTaskType is not None
        assert ModelAgentLearningRetrievalRequest is not None
        assert ModelAgentLearningRetrievalResponse is not None
        assert ModelRetrievedLearning is not None


@pytest.mark.unit
def test_all_five_entry_points_registered() -> None:
    """Each migrated node must be discoverable via the onex.nodes entry point group."""
    from importlib.metadata import entry_points

    eps = {ep.name for ep in entry_points(group="onex.nodes")}
    for name in NODE_NAMES:
        assert name in eps, f"{name} missing from onex.nodes entry points"
