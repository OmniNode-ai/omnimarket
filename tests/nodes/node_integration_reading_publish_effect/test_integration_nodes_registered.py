# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Both integration nodes are registered in the onex.nodes table (OMN-23899 AC3): a node that is not registered never loads.

Read from pyproject.toml itself, not from the installed distribution's metadata, so a stale install cannot hide a
missing line. The control drops each entry and shows this read catch it.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
NODES = (
    "node_integration_projection_compute",
    "node_integration_reading_publish_effect",
)


def _entries(text: str) -> dict[str, str]:
    return dict(tomllib.loads(text)["project"]["entry-points"]["onex.nodes"])


def _missing(text: str) -> list[str]:
    """The integration nodes the table does not register under their own package."""
    entries = _entries(text)
    return [node for node in NODES if entries.get(node) != f"omnimarket.nodes.{node}"]


def test_both_nodes_are_registered_under_their_own_package() -> None:
    assert _missing((REPO_ROOT / "pyproject.toml").read_text()) == []


@pytest.mark.parametrize("node", NODES)
def test_named_break_a_dropped_entry_is_caught_here_and_by_the_drift_validator(
    node: str,
) -> None:
    """Both nodes are lifecycle experimental, which no longer excuses a node from the drift validator's pyproject_entry
    check (OMN-24354: only the reviewed list does), so a dropped line fails there too as well as in this read."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    line = f'{node} = "omnimarket.nodes.{node}"\n'
    assert line in text
    assert _missing(text.replace(line, "")) == [node]
    drift = _drift()
    node_dir = REPO_ROOT / "src" / "omnimarket" / "nodes" / node
    registered = set(_entries(text))
    assert not drift.validate_node(node_dir, registered - {node}).passed


@pytest.mark.parametrize("node", NODES)
def test_the_entry_loads_the_node_package(node: str) -> None:
    module = importlib.import_module(f"omnimarket.nodes.{node}")
    assert any(name.startswith("Node") for name in module.__all__)


def _drift() -> ModuleType:
    name = "validate_node_drift_omn23899"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "validate_node_drift.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
