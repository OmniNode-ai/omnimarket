# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The whole-document overlay model: the example file loads, and wrong shapes are refused."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    ModelFloorFillLiveSettings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_overlay_document import (
    ModelFloorFillOverlayDocument,
)

NODE_DIR = Path(__file__).parents[1]
EXAMPLE = NODE_DIR / "overlay.example.yaml"


def example_document() -> dict[str, Any]:
    text = EXAMPLE.read_text(encoding="utf-8")
    document: Any = yaml.safe_load(text)
    assert isinstance(document, dict)
    return document


def test_document_loads_the_example() -> None:
    document = example_document()
    doc = ModelFloorFillOverlayDocument.model_validate(document)
    assert isinstance(doc.floor_fill_live, ModelFloorFillLiveSettings)
    assert doc.floor_fill_live == ModelFloorFillLiveSettings.model_validate(
        document["floor_fill_live"]
    )
    assert doc.floor_fill.scope


def test_document_missing_live_section_is_refused() -> None:
    document = example_document()
    del document["floor_fill_live"]
    with pytest.raises(ValidationError, match="floor_fill_live"):
        ModelFloorFillOverlayDocument.model_validate(document)


def test_document_unknown_section_is_refused() -> None:
    document = example_document()
    document["floor_fill_extra"] = {}
    with pytest.raises(ValidationError, match="floor_fill_extra"):
        ModelFloorFillOverlayDocument.model_validate(document)
