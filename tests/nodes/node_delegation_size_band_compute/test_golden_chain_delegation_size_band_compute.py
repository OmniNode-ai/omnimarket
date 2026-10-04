# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20167: contract-resolved size request -> compute -> typed result."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter

import omnimarket.nodes.node_delegation_size_band_compute as node_package
from omnimarket.models.delegation.model_size_band import (
    ModelSizeBand,
    ModelSizeBandRefusal,
    ModelSizeBandRequest,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
CLASS = "summarization"
TERMINAL_TOPIC = "onex.evt.omnimarket.delegation-size-band-completed.v1"


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_contract_binds_the_models_and_the_handler_and_the_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    handler_binding = contract["handler"]
    handler_type = getattr(
        importlib.import_module(handler_binding["module"]), handler_binding["class"]
    )
    assert issubclass(node_package.NodeDelegationSizeBandCompute, handler_type)
    input_model = getattr(
        importlib.import_module(contract["input_model"]["module"]),
        contract["input_model"]["name"],
    )
    assert input_model is ModelSizeBandRequest
    entry_points = (ROOT / "pyproject.toml").read_text()
    assert (
        'node_delegation_size_band_compute = "omnimarket.nodes.'
        'node_delegation_size_band_compute"' in entry_points
    )


def test_golden_chain_json_round_trip() -> None:
    contract = _contract()
    request = ModelSizeBandRequest.model_validate_json(
        json.dumps({"task_class": CLASS, "prompt": "Summarise.", "units": 1})
    )
    result = node_package.NodeDelegationSizeBandCompute().handle(request)
    assert isinstance(result, ModelSizeBandRefusal)
    assert result.fields == ("units",)
    adapter: TypeAdapter[Any] = TypeAdapter(
        getattr(
            importlib.import_module(contract["output_model"]["module"]),
            contract["output_model"]["name"],
        )
    )
    assert adapter.validate_json(result.model_dump_json()) == result
    accepted = node_package.NodeDelegationSizeBandCompute().handle(
        ModelSizeBandRequest(task_class=CLASS, prompt="Summarise.")
    )
    assert adapter.validate_json(accepted.model_dump_json()) == accepted
    assert isinstance(accepted, ModelSizeBand)
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["externally_consumed_topics"] == [TERMINAL_TOPIC]
