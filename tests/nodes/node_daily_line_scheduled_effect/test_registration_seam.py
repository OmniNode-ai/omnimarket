# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_daily_line_scheduled_effect is registered as an onex.nodes entry point (OMN-25163).

The broker ACL seam test finds scheduled job nodes by globbing node directories, so it never reads pyproject.toml: a node
whose directory exists but whose entry point was dropped would still pass it. This seam reads the entry point itself.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_daily_line_scheduled_effect"
PACKAGE = f"omnimarket.nodes.{NODE}"


def entry_points() -> dict[str, str]:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return dict(project["project"]["entry-points"]["onex.nodes"])


def test_seam_pyproject_registers_the_node_as_an_onex_nodes_entry_point() -> None:
    assert entry_points().get(NODE) == PACKAGE


def test_seam_the_registered_package_imports_and_exposes_the_node_class() -> None:
    module = importlib.import_module(entry_points()[NODE])
    assert hasattr(module, "NodeDailyLineScheduledEffect")


def test_seam_metadata_names_the_same_entry_point_as_pyproject() -> None:
    metadata = yaml.safe_load(
        (REPO / "src/omnimarket/nodes" / NODE / "metadata.yaml").read_text("utf-8")
    )
    assert metadata["entry_points"]["onex.nodes"] == {NODE: PACKAGE}


def test_control_seam_an_entry_point_table_without_the_node_is_detected() -> None:
    table = {k: v for k, v in entry_points().items() if k != NODE}
    assert table.get(NODE) is None
    assert len(table) == len(entry_points()) - 1
