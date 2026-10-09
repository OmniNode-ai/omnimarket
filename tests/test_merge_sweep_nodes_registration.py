# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract test: the merge-sweep reading and effect nodes are registered in package form (OMN-20676)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_metadata import MetadataSchema

_REPO_ROOT = Path(__file__).parent.parent
_NODES = ("node_merge_sweep_reading_compute", "node_merge_sweep_effect")


@pytest.mark.unit
@pytest.mark.parametrize("node", _NODES)
def test_entry_point_is_package_form(node: str) -> None:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    entry_points = data["project"]["entry-points"]["onex.nodes"]

    assert entry_points.get(node) == f"omnimarket.nodes.{node}"


@pytest.mark.unit
@pytest.mark.parametrize("node", _NODES)
def test_metadata_validates(node: str) -> None:
    meta_path = _REPO_ROOT / "src" / "omnimarket" / "nodes" / node / "metadata.yaml"
    with meta_path.open() as f:
        schema = MetadataSchema(**yaml.safe_load(f))

    assert schema.name == node
