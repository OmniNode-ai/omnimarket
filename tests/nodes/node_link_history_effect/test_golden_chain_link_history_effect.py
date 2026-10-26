# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Invoke the packaged contract with a typed request (OMN-21373)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel

pytestmark = pytest.mark.unit


def test_contract_golden_chain(tmp_path: Path) -> None:
    package = importlib.import_module("omnimarket.nodes.node_link_history_effect")
    assert package.__file__ is not None
    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    metadata = yaml.safe_load(
        (Path(package.__file__).parent / "metadata.yaml").read_text()
    )
    assert contract["name"] == "node_link_history_effect"
    assert contract["lifecycle"] == "experimental"
    assert contract["metadata"]["transport_type"] == "local"
    assert contract["metadata"]["related_tickets"] == ["OMN-21373"]
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
    handler = handler_class()
    history = str(tmp_path / "link-history.json")
    first = handler.handle(
        request_class(path=history, losses={"host-a": 40.0}, now=1_000_000.0)
    )
    assert first.priors == {"host-a": ()}
    second = handler.handle(
        request_class(path=history, losses={"host-a": 30.0}, now=1_000_120.0)
    )
    assert isinstance(second, result_class)
    assert second.priors == {"host-a": (40.0,)}
