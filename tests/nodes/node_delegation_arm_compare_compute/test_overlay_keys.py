# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged overlay.example.yaml validates and names every field of ModelArmCompareOverlay."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.delegation_arm_compare.model_arm_compare_overlay import (
    ModelArmCompareOverlay,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_delegation_arm_compare_compute/overlay.example.yaml"
)


def _example() -> dict[str, object]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_the_example_overlay_validates_and_names_every_key() -> None:
    overlay = ModelArmCompareOverlay.model_validate(_example())
    assert set(_example()) == set(ModelArmCompareOverlay.model_fields)
    assert overlay.line is not None


@pytest.mark.parametrize("key", sorted(ModelArmCompareOverlay.model_fields))
def test_a_missing_overlay_key_is_refused_not_defaulted(key: str) -> None:
    values = _example()
    del values[key]
    with pytest.raises(ValidationError):
        ModelArmCompareOverlay.model_validate(values)


def test_an_unknown_overlay_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelArmCompareOverlay.model_validate({**_example(), "surprise": 1})
