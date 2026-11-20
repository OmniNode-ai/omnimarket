# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the release cut tick: the contract names the handler and models, and a request payload becomes a typed result."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_release_cut_tick_effect as node_package
from omnimarket.nodes.node_release_cut_tick_effect.models.model_release_cut_tick import (
    ModelReleaseCutTickResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
NOW = "2026-09-29T13:00:00Z"


def _load(dotted: str) -> Any:
    module, _, name = dotted.rpartition(".")
    return getattr(importlib.import_module(module), name)


def test_golden_chain_dry_run_through_the_contract_names_the_block(
    tmp_path: Path,
) -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    handler = _load(f"{contract['handler']['module']}.{contract['handler']['class']}")()
    request_model = _load(contract["handler"]["input_model"])
    registry = tmp_path / "registry"
    registry.mkdir()
    request = request_model.model_validate(
        {
            "registry_root": str(registry),
            "ledger_path": str(tmp_path / "ledger.md"),
            "now": NOW,
            "dry_run": True,
        }
    )
    result = handler.handle(request)
    assert isinstance(result, ModelReleaseCutTickResult)
    assert (
        type(result).__module__ + "." + type(result).__name__
        == (contract["handler"]["output_model"])
    )
    # No clone is present, so the tick names the block and decides on nothing stale.
    assert result.outcome.action == "blocked"
    assert result.outcome.reason == "train_inputs_unavailable"
    assert result.status == "partial"
    assert result.dry_run is True
    assert not (registry / ".onex_state").exists()
