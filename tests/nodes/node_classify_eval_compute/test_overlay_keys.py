# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged overlay.example.yaml validates and names every field of ModelClassifyEvalOverlay."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.eval_classify.model_classify_overlay import (
    ModelClassifyEvalOverlay,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_classify_eval_compute/overlay.example.yaml"
)


def test_the_example_overlay_validates_and_names_every_key() -> None:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    overlay = ModelClassifyEvalOverlay.model_validate(document)
    assert set(document) == set(ModelClassifyEvalOverlay.model_fields)
    assert sum(overlay.per_class_n.values()) >= overlay.min_total


def test_a_key_the_schema_does_not_name_is_refused() -> None:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["extra_key"] = 1
    with pytest.raises(ValueError, match="extra_key"):
        ModelClassifyEvalOverlay.model_validate(document)
