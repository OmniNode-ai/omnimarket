# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node is a pure COMPUTE_GENERIC: contract, handler shape, no I/O, no clock."""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest
import yaml

from omnimarket.models.federation_egress.model_egress_gate import (
    ModelEgressGateRequest,
    ModelEgressGateResult,
)
from omnimarket.nodes.node_federation_egress_gate_compute import (
    NodeFederationEgressGateCompute,
)
from omnimarket.nodes.node_federation_egress_gate_compute.handlers import (
    handler_federation_egress_gate,
)
from omnimarket.nodes.node_federation_egress_gate_compute.handlers.handler_federation_egress_gate import (
    HandlerFederationEgressGate,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(handler_federation_egress_gate.__file__).resolve().parents[1]


def test_contract_declares_a_pure_compute_with_the_typed_models() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_federation_egress_gate_compute"
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["descriptor"]["purity"] == "pure"
    for side, model in (
        ("input_model", ModelEgressGateRequest),
        ("output_model", ModelEgressGateResult),
    ):
        declared = contract[side]
        module = importlib.import_module(declared["module"])
        assert getattr(module, declared["name"]) is model
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerFederationEgressGate
    )


def test_handler_is_sync_with_the_contract_signature() -> None:
    signature = inspect.signature(HandlerFederationEgressGate.handle)
    params = list(signature.parameters.values())
    assert [param.name for param in params] == ["self", "request"]
    assert params[1].annotation in (ModelEgressGateRequest, "ModelEgressGateRequest")
    assert signature.return_annotation in (
        ModelEgressGateResult,
        "ModelEgressGateResult",
    )
    assert not inspect.iscoroutinefunction(HandlerFederationEgressGate.handle)
    assert issubclass(NodeFederationEgressGateCompute, HandlerFederationEgressGate)


def test_handler_source_imports_no_io_and_reads_no_clock() -> None:
    source = Path(handler_federation_egress_gate.__file__).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            imported.add(node.module)
    forbidden = {
        "socket",
        "requests",
        "subprocess",
        "os",
        "time",
        "datetime",
        "random",
    }
    assert not imported & forbidden, imported & forbidden
    assert ".now(" not in source
    assert "utcnow" not in source
    assert "monotonic" not in source
    assert "perf_counter" not in source


def test_contract_declares_both_terminal_events_and_publishes_them() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    completed = "onex.evt.omnimarket.federation-egress-gate-completed.v1"
    failed = "onex.evt.omnimarket.federation-egress-gate-failed.v1"
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": completed,
        "failure": failed,
    }
    assert contract["event_bus"]["publish_topics"] == [completed, failed]
