# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered sampler resolves to the typed compute handler."""

import tomllib
from importlib import import_module
from pathlib import Path

import pytest
import yaml

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.nodes.node_delegation_eval_sample_compute.handlers import (
    handler_delegation_eval_sample as sampler,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_sample_request import (
    ModelDelegationEvalSampleRequest,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_sampling_config import (
    ModelDelegationEvalSamplingConfig,
)

pytestmark = pytest.mark.unit


def test_registered_eval_sample_route() -> None:
    route = _resolve_node_route("node_delegation_eval_sample_compute")
    assert (
        route.command_topic == "onex.cmd.omnimarket.delegation-eval-sample-requested.v1"
    )
    assert route.terminal_topic == "onex.evt.omnimarket.delegation-eval-sampled.v1"
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is sampler.HandlerDelegationEvalSample
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelDelegationEvalSampleRequest
    )
    sampling = ModelDelegationEvalSampleRequest.model_fields["sampling"]
    assert sampling.is_required()
    assert sampling.annotation is ModelDelegationEvalSamplingConfig


def test_eval_sample_contract_and_entry_point() -> None:
    root = Path(__file__).parents[3]
    node = root / "src/omnimarket/nodes/node_delegation_eval_sample_compute"
    contract = yaml.safe_load((node / "contract.yaml").read_text())
    metadata = yaml.safe_load((node / "metadata.yaml").read_text())
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["inputs"]["sampling"]["required"] is True
    assert contract["inputs"]["sampling"]["type"] == "ModelDelegationEvalSamplingConfig"
    assert (
        contract["handler_routing"]["handlers"][0]["operation"]
        == "delegation_eval_sample"
    )
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    name = "node_delegation_eval_sample_compute"
    assert (
        project["project"]["entry-points"]["onex.nodes"][name]
        == metadata["entry_points"]["onex.nodes"][name]
        == f"omnimarket.nodes.{name}"
    )
