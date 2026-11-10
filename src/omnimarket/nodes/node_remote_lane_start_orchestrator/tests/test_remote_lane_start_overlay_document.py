# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay document the overlays repository loads (OMN-23472): both sections, every key, no extras.

The overlays repository pins this model and loads ``overlays/node_remote_lane_start_orchestrator/overlay.yaml``
through it, so a key added, renamed or dropped here turns that load red until the overlay follows.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    ModelRemoteLaneStartOverlay,
    ModelRemoteLaneStartSettings,
)

pytestmark = pytest.mark.unit

EXAMPLE = Path(__file__).resolve().parents[1] / "overlay.example.yaml"


def _document() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    return document


def test_the_example_overlay_loads_as_the_whole_document() -> None:
    loaded = ModelRemoteLaneStartOverlay.model_validate(_document())

    assert loaded.floor_fill.runner_command[:2] == ("onex", "run-node")
    assert loaded.remote_lane_start == ModelRemoteLaneStartSettings.from_overlay(
        _document()
    )


def test_the_example_overlay_loads_the_way_the_overlays_repository_loads_it(
    tmp_path: Path,
) -> None:
    node = "node_remote_lane_start_orchestrator"
    (tmp_path / node).mkdir()
    (tmp_path / node / "overlay.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"))

    loaded = load_structured_overlay(
        node, ModelRemoteLaneStartOverlay, {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    )

    assert loaded.remote_lane_start.supported_engines == ("claude",)


@pytest.mark.parametrize("section", ["remote_lane_start", "floor_fill"])
def test_a_missing_section_is_refused(section: str) -> None:
    document = _document()
    del document[section]

    with pytest.raises(ValidationError, match=section):
        ModelRemoteLaneStartOverlay.model_validate(document)


def test_a_missing_settings_key_is_refused_naming_it() -> None:
    document = copy.deepcopy(_document())
    del document["remote_lane_start"]["claim_text"]

    with pytest.raises(ValidationError, match="claim_text"):
        ModelRemoteLaneStartOverlay.model_validate(document)


def test_an_unfillable_template_is_refused_at_load() -> None:
    document = copy.deepcopy(_document())
    document["remote_lane_start"]["engine_argv"] = ["claude", "-p", "{brief}", "{nope}"]

    with pytest.raises(ValidationError, match="engine_argv"):
        ModelRemoteLaneStartOverlay.model_validate(document)


def test_an_unknown_key_is_refused() -> None:
    document = copy.deepcopy(_document())
    document["remote_lane_start"]["surprise"] = "x"

    with pytest.raises(ValidationError, match="surprise"):
        ModelRemoteLaneStartOverlay.model_validate(document)
