# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Invoke the packaged contract with a typed request (OMN-21328)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel

pytestmark = pytest.mark.unit


def test_contract_golden_chain(tmp_path: Path) -> None:
    package = importlib.import_module("omnimarket.nodes.node_clone_parity_compute")
    assert package.__file__ is not None
    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    metadata = yaml.safe_load(
        (Path(package.__file__).parent / "metadata.yaml").read_text()
    )
    assert contract["name"] == "node_clone_parity_compute"
    assert contract["lifecycle"] == "experimental"
    assert contract["metadata"]["transport_type"] == "local"
    assert contract["metadata"]["related_tickets"] == ["OMN-21328"]
    assert (
        contract["runtime_profiles"] == contract["descriptor"]["runtime_profiles"] == []
    )
    assert metadata["pack"] == "distributed_validation"
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )
    assert (
        contract["handler_routing"]["default_handler"]
        == binding["module"] + ":" + binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert issubclass(result, BaseModel)
        assert result.model_config == {"frozen": True, "extra": "forbid"}
        return result

    request_class = model(binding["input_model"])
    result_class = model(binding["output_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    result = handler_class().handle(request_class(name="repo-a", path="/srv/elsewhere"))
    assert isinstance(result, result_class)
    assert result.failure_item == "core-worktree:repo-a"
    assert "/srv/elsewhere" in result.failure_reason
    assert "PARITY-CLONE" in result.clone_parity_py
