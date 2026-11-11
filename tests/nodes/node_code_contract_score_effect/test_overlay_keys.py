# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every value of node_code_contract_score_effect is an overlay key, and none has a default (OMN-23626)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_code_contract_score_effect.models import (
    ModelCodeScoreOverlay,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_code_contract_score_effect/overlay.example.yaml"
)


def _example() -> dict[str, object]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_the_example_overlay_validates_and_names_every_key() -> None:
    overlay = ModelCodeScoreOverlay.model_validate(_example())
    assert set(_example()) == set(ModelCodeScoreOverlay.model_fields)
    assert overlay.runnable_check_types == ("test_passes", "command")


@pytest.mark.parametrize("key", sorted(ModelCodeScoreOverlay.model_fields))
def test_a_missing_overlay_key_is_refused_not_defaulted(key: str) -> None:
    values = _example()
    del values[key]
    with pytest.raises(ValidationError):
        ModelCodeScoreOverlay.model_validate(values)


def test_an_unknown_overlay_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelCodeScoreOverlay.model_validate({**_example(), "surprise": 1})
