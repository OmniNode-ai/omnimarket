# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility tests for persona, navigation, and learning DTO imports."""

from __future__ import annotations

from importlib import import_module, resources
from pathlib import Path
from typing import Any

import pytest
import yaml

DTO_IMPORTS = {
    "omnimarket.nodes.node_agent_learning_retrieval_effect.models.model_request.EnumRetrievalMatchType": "omnimemory.nodes.node_agent_learning_retrieval_effect.models.model_request.EnumRetrievalMatchType",
    "omnimarket.nodes.node_agent_learning_retrieval_effect.models.model_request.ModelAgentLearningRetrievalRequest": "omnimemory.nodes.node_agent_learning_retrieval_effect.models.model_request.ModelAgentLearningRetrievalRequest",
    "omnimarket.nodes.node_agent_learning_retrieval_effect.models.model_response.EnumRetrievalTaskType": "omnimemory.nodes.node_agent_learning_retrieval_effect.models.model_response.EnumRetrievalTaskType",
    "omnimarket.nodes.node_agent_learning_retrieval_effect.models.model_response.ModelAgentLearningRetrievalResponse": "omnimemory.nodes.node_agent_learning_retrieval_effect.models.model_response.ModelAgentLearningRetrievalResponse",
    "omnimarket.nodes.node_agent_learning_retrieval_effect.models.model_response.ModelRetrievedLearning": "omnimemory.nodes.node_agent_learning_retrieval_effect.models.model_response.ModelRetrievedLearning",
    "omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_history_request.ModelNavigationHistoryRequest": "omnimemory.nodes.node_navigation_history_reducer.models.model_navigation_history_request.ModelNavigationHistoryRequest",
    "omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_history_response.ModelNavigationHistoryResponse": "omnimemory.nodes.node_navigation_history_reducer.models.model_navigation_history_response.ModelNavigationHistoryResponse",
    "omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_session.EnumNavigationOutcomeTag": "omnimemory.nodes.node_navigation_history_reducer.models.model_navigation_session.EnumNavigationOutcomeTag",
    "omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_session.ModelNavigationOutcomeFailure": "omnimemory.nodes.node_navigation_history_reducer.models.model_navigation_session.ModelNavigationOutcomeFailure",
    "omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_session.ModelNavigationOutcomeSuccess": "omnimemory.nodes.node_navigation_history_reducer.models.model_navigation_session.ModelNavigationOutcomeSuccess",
    "omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_session.ModelNavigationSession": "omnimemory.nodes.node_navigation_history_reducer.models.model_navigation_session.ModelNavigationSession",
    "omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_session.ModelPlanStep": "omnimemory.nodes.node_navigation_history_reducer.models.model_navigation_session.ModelPlanStep",
    "omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_session.NavigationOutcome": "omnimemory.nodes.node_navigation_history_reducer.models.model_navigation_session.NavigationOutcome",
    "omnimarket.nodes.node_persona_builder_compute.models.model_classify_request.ModelPersonaClassifyRequest": "omnimemory.nodes.node_persona_builder_compute.models.model_classify_request.ModelPersonaClassifyRequest",
    "omnimarket.nodes.node_persona_builder_compute.models.model_classify_result.ModelPersonaClassifyResult": "omnimemory.nodes.node_persona_builder_compute.models.model_classify_result.ModelPersonaClassifyResult",
    "omnimarket.nodes.node_persona_lifecycle_orchestrator.models.model_persona_lifecycle_request.ModelPersonaLifecycleRequest": "omnimemory.nodes.node_persona_lifecycle_orchestrator.models.model_persona_lifecycle_request.ModelPersonaLifecycleRequest",
    "omnimarket.nodes.node_persona_lifecycle_orchestrator.models.model_persona_lifecycle_response.ModelPersonaLifecycleResponse": "omnimemory.nodes.node_persona_lifecycle_orchestrator.models.model_persona_lifecycle_response.ModelPersonaLifecycleResponse",
    "omnimarket.nodes.node_persona_retrieval_effect.models.model_persona_retrieval_request.ModelPersonaRetrievalRequest": "omnimemory.nodes.node_persona_retrieval_effect.models.model_persona_retrieval_request.ModelPersonaRetrievalRequest",
    "omnimarket.nodes.node_persona_retrieval_effect.models.model_persona_retrieval_response.ModelPersonaRetrievalResponse": "omnimemory.nodes.node_persona_retrieval_effect.models.model_persona_retrieval_response.ModelPersonaRetrievalResponse",
    "omnimarket.nodes.node_persona_storage_effect.models.model_persona_storage_request.ModelPersonaStorageRequest": "omnimemory.nodes.node_persona_storage_effect.models.model_persona_storage_request.ModelPersonaStorageRequest",
    "omnimarket.nodes.node_persona_storage_effect.models.model_persona_storage_response.ModelPersonaStorageResponse": "omnimemory.nodes.node_persona_storage_effect.models.model_persona_storage_response.ModelPersonaStorageResponse",
}


def _resolve(dotted_ref: str) -> Any:
    module_name, _, attr = dotted_ref.rpartition(".")
    return getattr(import_module(module_name), attr)


@pytest.mark.unit
@pytest.mark.parametrize(("market_ref", "memory_ref"), DTO_IMPORTS.items())
def test_market_dto_paths_reexport_omnimemory_objects(
    market_ref: str, memory_ref: str
) -> None:
    """Existing omnimarket module paths must resolve to canonical DTO objects."""
    market_model = _resolve(market_ref)
    memory_model = _resolve(memory_ref)

    assert market_model is memory_model
    if hasattr(market_model, "__module__") and market_model.__module__ != "types":
        assert market_model.__module__.startswith("omnimemory.")


@pytest.mark.unit
def test_node_package_exports_reexport_omnimemory_classes() -> None:
    """Node-level compatibility exports return canonical omnimemory classes."""
    from omnimarket.nodes.node_agent_learning_retrieval_effect import (
        ModelAgentLearningRetrievalRequest,
        ModelAgentLearningRetrievalResponse,
    )
    from omnimarket.nodes.node_navigation_history_reducer import (
        ModelNavigationHistoryRequest,
        ModelNavigationHistoryResponse,
        ModelNavigationSession,
    )
    from omnimarket.nodes.node_persona_builder_compute.models import (
        ModelPersonaClassifyRequest,
        ModelPersonaClassifyResult,
    )
    from omnimarket.nodes.node_persona_lifecycle_orchestrator import (
        ModelPersonaLifecycleRequest,
        ModelPersonaLifecycleResponse,
    )
    from omnimarket.nodes.node_persona_retrieval_effect import (
        ModelPersonaRetrievalRequest,
        ModelPersonaRetrievalResponse,
    )
    from omnimarket.nodes.node_persona_storage_effect import (
        ModelPersonaStorageRequest,
        ModelPersonaStorageResponse,
    )

    exported_models = [
        ModelAgentLearningRetrievalRequest,
        ModelAgentLearningRetrievalResponse,
        ModelNavigationHistoryRequest,
        ModelNavigationHistoryResponse,
        ModelNavigationSession,
        ModelPersonaClassifyRequest,
        ModelPersonaClassifyResult,
        ModelPersonaLifecycleRequest,
        ModelPersonaLifecycleResponse,
        ModelPersonaRetrievalRequest,
        ModelPersonaRetrievalResponse,
        ModelPersonaStorageRequest,
        ModelPersonaStorageResponse,
    ]

    assert all(model.__module__.startswith("omnimemory.") for model in exported_models)


@pytest.mark.unit
@pytest.mark.parametrize(
    "node_name",
    [
        "node_agent_learning_retrieval_effect",
        "node_persona_builder_compute",
    ],
)
def test_contract_model_refs_resolve_to_canonical_classes(node_name: str) -> None:
    """Contract schema refs can keep omnimarket paths through compatibility shims."""
    contract_path = Path(
        resources.files("omnimarket.nodes") / node_name / "contract.yaml"  # type: ignore[arg-type]
    )
    contract = yaml.safe_load(contract_path.read_text())
    refs = _contract_model_refs(contract)

    assert refs
    assert all(_resolve(ref).__module__.startswith("omnimemory.") for ref in refs)


def _contract_model_refs(contract: dict[str, Any]) -> list[str]:
    refs: list[str] = []

    for key in ("input_model", "output_model"):
        model_ref = contract.get(key)
        if (
            isinstance(model_ref, dict)
            and "module" in model_ref
            and "name" in model_ref
        ):
            refs.append(f"{model_ref['module']}.{model_ref['name']}")

    event_bus = contract.get("event_bus", {})
    for metadata_key in ("subscribe_topic_metadata", "publish_topic_metadata"):
        for metadata in event_bus.get(metadata_key, {}).values():
            if "schema_ref" in metadata:
                refs.append(metadata["schema_ref"])

    return refs
