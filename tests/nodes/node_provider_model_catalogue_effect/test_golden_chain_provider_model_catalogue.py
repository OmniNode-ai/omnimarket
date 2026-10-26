# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed handler replay of the recorded provider catalogue."""

from pathlib import Path

import pytest

from omnimarket.nodes.node_provider_model_catalogue_effect import (
    HandlerProviderModelCatalogue,
)
from omnimarket.nodes.node_provider_model_catalogue_effect.models.model_provider_model_catalogue import (
    ModelProviderModelCatalogueRequest,
)

pytestmark = pytest.mark.unit


def test_golden_chain_catalogue() -> None:
    fixture = Path(__file__).parents[2] / "fixtures/openrouter_models_2026_10_25.json"
    result = HandlerProviderModelCatalogue().handle(
        ModelProviderModelCatalogueRequest(catalogue_json=fixture)
    )
    assert result.missing == {}
