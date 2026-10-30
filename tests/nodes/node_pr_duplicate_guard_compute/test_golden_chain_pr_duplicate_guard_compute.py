# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node is registered, and its packaged contract invokes with a typed request (OMN-17427)."""

from __future__ import annotations

import importlib
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

NODE = "node_pr_duplicate_guard_compute"
PACKAGE = f"omnimarket.nodes.{NODE}"
REPO = Path(__file__).resolve().parents[3]


def test_node_is_registered_as_an_onex_node() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["entry-points"]["onex.nodes"][NODE] == PACKAGE
    package = importlib.import_module(PACKAGE)
    assert package.NodePrDuplicateGuardCompute.contract_path.is_file()


def test_contract_names_the_handler_and_models_it_ships() -> None:
    package = importlib.import_module(PACKAGE)
    assert package.__file__ is not None
    root = Path(package.__file__).parent
    contract = yaml.safe_load((root / "contract.yaml").read_text(encoding="utf-8"))
    metadata = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))
    assert contract["name"] == metadata["name"] == NODE
    assert metadata["entry_points"]["onex.nodes"][NODE] == PACKAGE
    binding = contract["handler"]
    handler = getattr(importlib.import_module(binding["module"]), binding["class"])
    request_type = getattr(
        importlib.import_module(contract["input_model"]["module"]),
        contract["input_model"]["name"],
    )
    result = handler().handle(
        request_type(
            repo="OmniNode-ai/omnibase_core",
            head_ref="a-branch",
            now=datetime(2026, 10, 30, 1, 0, tzinfo=UTC),
        )
    )
    output_type = getattr(
        importlib.import_module(contract["output_model"]["module"]),
        contract["output_model"]["name"],
    )
    assert isinstance(result, output_type)
    assert result.state == "clear"


def test_snapshot_effect_node_is_registered_and_binds_its_handler() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    node = "node_pr_duplicate_snapshot_effect"
    assert (
        pyproject["project"]["entry-points"]["onex.nodes"][node]
        == f"omnimarket.nodes.{node}"
    )
    package = importlib.import_module(f"omnimarket.nodes.{node}")
    assert package.__file__ is not None
    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text(encoding="utf-8")
    )
    binding = contract["handler"]
    assert getattr(importlib.import_module(binding["module"]), binding["class"])
    assert contract["name"] == node
