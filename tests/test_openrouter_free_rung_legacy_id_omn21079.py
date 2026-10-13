# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OpenRouter free rung is named for the model it serves (OMN-21079).

The rung serves ``nvidia/nemotron-3-ultra-550b-a55b:free`` under the backend id
``openrouter-nemotron-ultra``. Receipts written before the id was renamed carry
the earlier id, and receipts are append-only, so exactly one legacy mapping
lives in the model registry. A reader that holds a receipt's backend id asks the
registry; it never carries a second spelling of its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.models.delegation.llm_cost_routing.model_llm_model_registry import (
    ModelLlmModelRegistry,
    ModelLlmModelRegistryLoader,
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_REGISTRY_FILE: Final[Path] = (
    _REPO_ROOT / "src/omnimarket/data/model_registry/model_registry_v1.yaml"
)

CURRENT_ID: Final[str] = "openrouter-nemotron-ultra"
SERVED_MODEL: Final[str] = "nvidia/nemotron-3-ultra-550b-a55b:free"
LEGACY_ID: Final[str] = "openrouter-qwen3-coder-480b"


@pytest.fixture(scope="module")
def registry() -> ModelLlmModelRegistry:
    return ModelLlmModelRegistryLoader().load()


@pytest.mark.unit
def test_routing_resolves_the_current_id_to_the_served_model() -> None:
    config = load_bifrost_delegation_config()
    served = {b.backend_id: b.model_name for b in config.backends}
    assert served[CURRENT_ID] == SERVED_MODEL
    assert LEGACY_ID not in served


@pytest.mark.unit
def test_registry_resolves_the_current_id_to_the_served_model(
    registry: ModelLlmModelRegistry,
) -> None:
    assert registry.get_model(CURRENT_ID).model_name == SERVED_MODEL
    assert registry.canonical_model_id(CURRENT_ID) == CURRENT_ID


@pytest.mark.unit
def test_a_receipt_written_under_the_legacy_id_maps_to_the_current_id(
    registry: ModelLlmModelRegistry, tmp_path: Path
) -> None:
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps({"backend_id": LEGACY_ID, "requested_backend_id": LEGACY_ID})
    )
    receipt = json.loads(receipt_path.read_text())

    assert registry.canonical_model_id(receipt["backend_id"]) == CURRENT_ID
    assert registry.get_model(receipt["backend_id"]).model_name == SERVED_MODEL
    # Positive control: the receipt really carries the id the registry no longer keys.
    assert receipt["backend_id"] not in registry.models


@pytest.mark.unit
def test_there_is_exactly_one_legacy_mapping(
    registry: ModelLlmModelRegistry,
) -> None:
    assert registry.legacy_model_ids == {LEGACY_ID: CURRENT_ID}


@pytest.mark.unit
def test_an_unknown_id_is_not_mapped_and_not_found(
    registry: ModelLlmModelRegistry,
) -> None:
    assert registry.canonical_model_id("no-such-backend") == "no-such-backend"
    with pytest.raises(KeyError):
        registry.get_model("no-such-backend")


@pytest.mark.unit
def test_the_legacy_id_is_spelled_only_in_the_registry_mapping() -> None:
    """AC1: no source file other than the registry's mapping carries the old id."""
    hits = sorted(
        str(path.relative_to(_REPO_ROOT))
        for path in (_REPO_ROOT / "src").rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".yaml", ".yml", ".json", ".md", ".sql"}
        and LEGACY_ID in path.read_text(encoding="utf-8", errors="ignore")
    )
    assert hits == [str(_REGISTRY_FILE.relative_to(_REPO_ROOT))]
