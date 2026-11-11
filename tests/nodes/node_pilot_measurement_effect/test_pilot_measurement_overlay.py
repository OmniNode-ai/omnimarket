# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay loads, refuses what it must refuse, and hashes what the reading depends on."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_pilot_manifest_check_compute.handlers.handler_pilot_manifest_check import (
    compute_bar_hash,
)
from omnimarket.nodes.node_pilot_measurement_effect.overlay import (
    NODE_NAME,
    ModelPilotMeasurementOverlay,
    load_overlay,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_pilot_measurement_effect/overlay.example.yaml"
)


def example_document() -> dict[str, Any]:
    """The example overlay as a dict, exactly as group C's support module loads it."""
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def _root_with(document: dict[str, Any], tmp_path: Path) -> dict[str, str]:
    """Write ``document`` as the node's overlay under a root, and name that root."""
    home = tmp_path / NODE_NAME
    home.mkdir(parents=True, exist_ok=True)
    (home / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def test_the_example_overlay_loads(tmp_path: Path) -> None:
    overlay = load_overlay(_root_with(example_document(), tmp_path))
    assert [pilot.pilot for pilot in overlay.pilots] == [
        "example-pilot-refactor",
        "example-pilot-handoff",
    ]
    assert overlay.emission.piece == 16
    assert overlay.publish.rejected_reading == "NOT_WORKING"
    assert overlay.manifest_check.hash_algorithm == "sha256"


def test_the_example_bar_hashes_are_the_ones_the_check_computes() -> None:
    """A bar_hash in the example that the check would not compute is an invented one."""
    overlay = ModelPilotMeasurementOverlay.model_validate(example_document())
    for pilot in overlay.pilots:
        assert pilot.bar, pilot.pilot
        assert pilot.bar_hash == compute_bar_hash(pilot.bar)


def test_an_absent_key_refuses_the_load_and_names_it(tmp_path: Path) -> None:
    document = example_document()
    del document["publish"]
    with pytest.raises(NodeOverlayError) as refused:
        load_overlay(_root_with(document, tmp_path))
    assert "publish" in str(refused.value)


def test_an_absent_pilot_key_refuses_the_load_and_names_it(tmp_path: Path) -> None:
    document = example_document()
    del document["pilots"][0]["started_at"]
    with pytest.raises(NodeOverlayError) as refused:
        load_overlay(_root_with(document, tmp_path))
    assert "started_at" in str(refused.value)


def test_an_extra_key_refuses_the_load_and_names_it(tmp_path: Path) -> None:
    document = example_document()
    document["not_a_key"] = "value"
    with pytest.raises(NodeOverlayError) as refused:
        load_overlay(_root_with(document, tmp_path))
    assert "not_a_key" in str(refused.value)


def test_duplicate_pilot_names_refuse_the_load() -> None:
    document = example_document()
    document["pilots"][1]["pilot"] = document["pilots"][0]["pilot"]
    with pytest.raises(ValidationError) as refused:
        ModelPilotMeasurementOverlay.model_validate(document)
    assert "a pilot is declared twice" in str(refused.value)


def test_overlay_hash_changes_when_a_threshold_changes() -> None:
    document = example_document()
    overlay_a = ModelPilotMeasurementOverlay.model_validate(document)
    document["pilots"][0]["bar"][0]["threshold"] = 0.95
    overlay_b = ModelPilotMeasurementOverlay.model_validate(document)
    assert overlay_a.overlay_hash() != overlay_b.overlay_hash()
