# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility tests for omnimarket memory DTO imports."""

from __future__ import annotations

from importlib import import_module, resources
from pathlib import Path
from typing import Any

import pytest
import yaml

DTO_IMPORTS = {
    "omnimarket.nodes.node_memory_retrieval_effect.models.model_embedding_client_config.ModelEmbeddingClientConfig": "omnimemory.nodes.node_memory_retrieval_effect.models.model_embedding_client_config.ModelEmbeddingClientConfig",
    "omnimarket.nodes.node_memory_retrieval_effect.models.model_handler_db_mock_config.ModelHandlerDbMockConfig": "omnimemory.nodes.node_memory_retrieval_effect.models.model_handler_db_mock_config.ModelHandlerDbMockConfig",
    "omnimarket.nodes.node_memory_retrieval_effect.models.model_handler_graph_mock_config.ModelHandlerGraphMockConfig": "omnimemory.nodes.node_memory_retrieval_effect.models.model_handler_graph_mock_config.ModelHandlerGraphMockConfig",
    "omnimarket.nodes.node_memory_retrieval_effect.models.model_handler_memory_retrieval_config.ModelHandlerMemoryRetrievalConfig": "omnimemory.nodes.node_memory_retrieval_effect.models.model_handler_memory_retrieval_config.ModelHandlerMemoryRetrievalConfig",
    "omnimarket.nodes.node_memory_retrieval_effect.models.model_handler_qdrant_config.ModelHandlerQdrantConfig": "omnimemory.nodes.node_memory_retrieval_effect.models.model_handler_qdrant_config.ModelHandlerQdrantConfig",
    "omnimarket.nodes.node_memory_retrieval_effect.models.model_handler_qdrant_mock_config.ModelHandlerQdrantMockConfig": "omnimemory.nodes.node_memory_retrieval_effect.models.model_handler_qdrant_mock_config.ModelHandlerQdrantMockConfig",
    "omnimarket.nodes.node_memory_retrieval_effect.models.model_memory_retrieval_request.ModelMemoryRetrievalRequest": "omnimemory.nodes.node_memory_retrieval_effect.models.model_memory_retrieval_request.ModelMemoryRetrievalRequest",
    "omnimarket.nodes.node_memory_retrieval_effect.models.model_memory_retrieval_response.ModelMemoryRetrievalResponse": "omnimemory.nodes.node_memory_retrieval_effect.models.model_memory_retrieval_response.ModelMemoryRetrievalResponse",
    "omnimarket.nodes.node_memory_retrieval_effect.models.model_memory_retrieval_response.ModelSearchResult": "omnimemory.nodes.node_memory_retrieval_effect.models.model_memory_retrieval_response.ModelSearchResult",
    "omnimarket.nodes.node_memory_storage_effect.models.model_memory_storage_request.ModelMemoryStorageRequest": "omnimemory.nodes.node_memory_storage_effect.models.model_memory_storage_request.ModelMemoryStorageRequest",
    "omnimarket.nodes.node_memory_storage_effect.models.model_memory_storage_response.ModelMemoryStorageResponse": "omnimemory.nodes.node_memory_storage_effect.models.model_memory_storage_response.ModelMemoryStorageResponse",
}


def _resolve(dotted_ref: str) -> Any:
    module_name, _, attr = dotted_ref.rpartition(".")
    return getattr(import_module(module_name), attr)


@pytest.mark.unit
@pytest.mark.parametrize(("market_ref", "memory_ref"), DTO_IMPORTS.items())
def test_memory_dto_market_paths_reexport_omnimemory_classes(
    market_ref: str, memory_ref: str
) -> None:
    """Existing omnimarket module paths must resolve to canonical omnimemory DTOs."""
    market_model = _resolve(market_ref)
    memory_model = _resolve(memory_ref)

    assert market_model is memory_model
    assert market_model.__module__.startswith("omnimemory.")


@pytest.mark.unit
def test_memory_dto_package_exports_reexport_omnimemory_classes() -> None:
    """Node-level exports keep compatibility while returning canonical classes."""
    from omnimarket.nodes.node_memory_retrieval_effect import (
        ModelHandlerMemoryRetrievalConfig,
        ModelMemoryRetrievalRequest,
        ModelMemoryRetrievalResponse,
        ModelSearchResult,
    )
    from omnimarket.nodes.node_memory_storage_effect import (
        ModelMemoryStorageRequest,
        ModelMemoryStorageResponse,
    )

    exported_models = [
        ModelHandlerMemoryRetrievalConfig,
        ModelMemoryRetrievalRequest,
        ModelMemoryRetrievalResponse,
        ModelSearchResult,
        ModelMemoryStorageRequest,
        ModelMemoryStorageResponse,
    ]

    assert all(model.__module__.startswith("omnimemory.") for model in exported_models)


@pytest.mark.unit
@pytest.mark.parametrize(
    "node_name",
    ["node_memory_retrieval_effect", "node_memory_storage_effect"],
)
def test_memory_effect_schema_refs_resolve_to_canonical_classes(node_name: str) -> None:
    """Contract schema refs can keep omnimarket paths through compatibility shims."""
    contract_path = Path(
        resources.files("omnimarket.nodes") / node_name / "contract.yaml"  # type: ignore[arg-type]
    )
    contract = yaml.safe_load(contract_path.read_text())
    event_bus = contract["event_bus"]

    schema_refs = [
        metadata["schema_ref"]
        for metadata_key in ("subscribe_topic_metadata", "publish_topic_metadata")
        for metadata in event_bus.get(metadata_key, {}).values()
        if "schema_ref" in metadata
    ]

    assert schema_refs
    assert all(
        _resolve(schema_ref).__module__.startswith("omnimemory.")
        for schema_ref in schema_refs
    )
