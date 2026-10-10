"""Contract test: the remote-lane nodes are registered in package form with valid metadata."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_metadata import MetadataSchema

_REPO_ROOT = Path(__file__).parent.parent
_NODES_DIR = _REPO_ROOT / "src" / "omnimarket" / "nodes"
_REMOTE_LANE_NODES = (
    "node_remote_lane_close_compute",
    "node_remote_lane_compute",
    "node_remote_lane_effect",
)


@pytest.mark.unit
@pytest.mark.parametrize("node_name", _REMOTE_LANE_NODES)
def test_remote_lane_entry_point_is_package_form(node_name: str) -> None:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    entry_points = data["project"]["entry-points"]["onex.nodes"]

    assert entry_points.get(node_name) == f"omnimarket.nodes.{node_name}"


@pytest.mark.unit
@pytest.mark.parametrize("node_name", _REMOTE_LANE_NODES)
def test_remote_lane_metadata_validates(node_name: str) -> None:
    meta_path = _NODES_DIR / node_name / "metadata.yaml"
    with meta_path.open() as f:
        schema = MetadataSchema(**yaml.safe_load(f))

    assert schema.name == node_name
