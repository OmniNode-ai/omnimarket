# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility checks for similarity DTOs canonicalized in omnimemory."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_similarity_compute.models import (
    ModelHandlerSimilarityComputeConfig,
    ModelSimilarityComputeRequest,
    ModelSimilarityComputeResponse,
)


@pytest.mark.unit
def test_similarity_dtos_are_canonical_omnimemory_imports() -> None:
    """Local omnimarket model paths re-export canonical omnimemory DTO classes."""
    from omnimemory.nodes.node_similarity_compute.models import (
        ModelHandlerSimilarityComputeConfig as CanonicalConfig,
    )
    from omnimemory.nodes.node_similarity_compute.models import (
        ModelSimilarityComputeRequest as CanonicalRequest,
    )
    from omnimemory.nodes.node_similarity_compute.models import (
        ModelSimilarityComputeResponse as CanonicalResponse,
    )

    assert ModelHandlerSimilarityComputeConfig is CanonicalConfig
    assert ModelSimilarityComputeRequest is CanonicalRequest
    assert ModelSimilarityComputeResponse is CanonicalResponse


@pytest.mark.unit
def test_similarity_contract_model_paths_still_resolve() -> None:
    """Contract module strings stay omnimarket-local while resolving canonical DTOs."""
    repo_root = Path(__file__).parent.parent
    contract_path = (
        repo_root
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_similarity_compute"
        / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())

    input_module = importlib.import_module(contract["input_model"]["module"])
    output_module = importlib.import_module(contract["output_model"]["module"])

    assert input_module.ModelSimilarityComputeRequest is ModelSimilarityComputeRequest
    assert (
        output_module.ModelSimilarityComputeResponse is ModelSimilarityComputeResponse
    )


@pytest.mark.unit
def test_similarity_dto_validation_and_schema_survive_reexport() -> None:
    """The canonical DTO schema and validators are preserved through omnimarket paths."""
    request = ModelSimilarityComputeRequest(
        operation="compare",
        vector_a=[1.0, 0.0],
        vector_b=[0.0, 1.0],
        threshold=0.8,
    )
    response = ModelSimilarityComputeResponse(
        status="success",
        distance=1.0,
        similarity=0.0,
        is_match=False,
        dimensions=2,
    )

    assert request.metric == "cosine"
    assert response.model_dump()["dimensions"] == 2
    assert ModelSimilarityComputeRequest.model_json_schema()["title"] == (
        "ModelSimilarityComputeRequest"
    )

    with pytest.raises(ValueError, match="Dimension mismatch"):
        ModelSimilarityComputeRequest(
            operation="compare",
            vector_a=[1.0],
            vector_b=[1.0, 2.0],
        )
