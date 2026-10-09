"""Contract test: the merge-sweep plan node is registered in package form with valid metadata."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_metadata import MetadataSchema

_REPO_ROOT = Path(__file__).parent.parent
_NODE = "node_merge_sweep_plan_compute"


@pytest.mark.unit
def test_merge_sweep_plan_entry_point_is_package_form() -> None:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    entry_points = data["project"]["entry-points"]["onex.nodes"]

    assert entry_points.get(_NODE) == f"omnimarket.nodes.{_NODE}"


@pytest.mark.unit
def test_merge_sweep_plan_metadata_validates() -> None:
    meta_path = _REPO_ROOT / "src" / "omnimarket" / "nodes" / _NODE / "metadata.yaml"
    with meta_path.open() as f:
        schema = MetadataSchema(**yaml.safe_load(f))

    assert schema.name == _NODE
