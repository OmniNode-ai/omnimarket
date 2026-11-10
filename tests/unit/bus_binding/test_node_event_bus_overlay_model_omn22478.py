# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model the overlay home validates an event-bus-only node overlay through (OMN-22478)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBusOverlay

pytestmark = pytest.mark.unit


def _write(root: Path, node: str, document: object) -> dict[str, str]:
    (root / node).mkdir(parents=True)
    (root / node / "overlay.yaml").write_text(
        yaml.safe_dump(document), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def test_loads_the_way_the_overlay_home_loads_it(tmp_path: Path) -> None:
    environ = _write(
        tmp_path, "node_a", {"event_bus": {"type": "kafka", "lane": "dev"}}
    )
    overlay = load_structured_overlay("node_a", ModelNodeEventBusOverlay, environ)
    assert overlay.event_bus.lane == "dev"


@pytest.mark.parametrize(
    "document",
    [
        {"event_bus": {"type": "kafka"}},
        {"event_bus": {"type": "inmemory", "lane": "dev"}},
        {"event_bus": {"type": "kafka", "lane": "dev"}, "stray": 1},
        {"stray": 1},
    ],
)
def test_a_wrong_document_refuses(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelNodeEventBusOverlay.model_validate(document)


def test_imports_with_pydantic_and_yaml_only() -> None:
    """The overlay home's check installs pydantic and pyyaml and nothing else."""
    code = (
        "import sys\n"
        "from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBusOverlay\n"
        "assert ModelNodeEventBusOverlay\n"
        "heavy = {'omnibase_core', 'omnibase_infra', 'omnibase_spi'} & set(sys.modules)\n"
        "assert not heavy, sorted(heavy)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
