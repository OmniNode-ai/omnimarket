# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-shape coverage for node_staging_readiness_compute (OMN-15253).

Asserts the node's declared surface directly, so the contract cannot drift away
from the handler it routes to:

* the archetype and purity declarations that make this a pure COMPUTE node;
* the definition-B routing (``input_model`` / ``handler.input_model`` both
  resolve to ``ModelStagingReadinessRequest``, and the handler's own signature
  matches — no ``ModelEventEnvelope``, no ``ModelHandlerOutput``);
* every declared command and terminal topic, each named as a literal so the
  contract-state-coverage gate can see it is exercised.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, get_type_hints

import pytest
import yaml

from omnimarket.nodes.node_staging_readiness_compute.handlers.handler_staging_readiness_compute import (
    HandlerStagingReadinessCompute,
)
from omnimarket.staging_readiness.model_staging_composition import (
    ModelStagingReadinessRequest,
    ModelStagingReadinessVerdict,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_staging_readiness_compute"
    / "contract.yaml"
)

COMMAND_TOPIC = "onex.cmd.omnimarket.staging-readiness-compute.v1"
SUCCESS_TOPIC = "onex.evt.omnimarket.staging-readiness-evaluated.v1"
FAILURE_TOPIC = "onex.evt.omnimarket.staging-readiness-failed.v1"


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    doc = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def test_contract_declares_a_pure_idempotent_compute_node(
    contract: dict[str, Any],
) -> None:
    assert contract["node_type"] == "compute"
    descriptor = contract["descriptor"]
    assert descriptor["node_archetype"] == "compute"
    assert descriptor["purity"] == "pure"
    assert descriptor["idempotent"] is True


def test_contract_routes_the_definition_b_signature(contract: dict[str, Any]) -> None:
    expected = (
        "omnimarket.staging_readiness.model_staging_composition."
        "ModelStagingReadinessRequest"
    )
    assert contract["handler"]["input_model"] == expected
    assert contract["input_model"]["name"] == "ModelStagingReadinessRequest"
    assert contract["output_model"]["name"] == "ModelStagingReadinessVerdict"

    signature = inspect.signature(HandlerStagingReadinessCompute.handle)
    parameters = list(signature.parameters.values())[1:]
    assert len(parameters) == 1, "def-B takes exactly one typed request"

    hints = get_type_hints(HandlerStagingReadinessCompute.handle)
    assert hints[parameters[0].name] is ModelStagingReadinessRequest
    assert hints["return"] is ModelStagingReadinessVerdict


def test_handler_module_never_imports_the_envelope_shape() -> None:
    """The pre-def-B shape hard-fails the OMN-14355 canon-shape ratchet.

    Checked against the module's IMPORTS, not its text: the docstrings in this
    package legitimately name the forbidden shapes when explaining why they are
    forbidden, and a substring scan would flag that prose.
    """
    module = inspect.getmodule(HandlerStagingReadinessCompute)
    assert module is not None
    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.update(alias.name for alias in node.names)

    assert "ModelEventEnvelope" not in imported
    assert "ModelHandlerOutput" not in imported


def test_contract_declares_its_command_and_terminal_topics(
    contract: dict[str, Any],
) -> None:
    dispatch = contract["runtime_dispatch"]
    assert dispatch["command_topic"] == COMMAND_TOPIC
    assert dispatch["terminal_events"]["success"] == SUCCESS_TOPIC
    assert dispatch["terminal_events"]["failure"] == FAILURE_TOPIC

    assert contract["terminal_event"] == SUCCESS_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [SUCCESS_TOPIC, FAILURE_TOPIC]


def test_terminal_topics_are_declared_externally_consumed(
    contract: dict[str, Any],
) -> None:
    """The verdict leaves the contract graph: the preflight CLI reads it today,
    and slice 3's fail-closed pre-deploy gate reads it next."""
    externally_consumed = contract["externally_consumed_topics"]
    assert SUCCESS_TOPIC in externally_consumed
    assert FAILURE_TOPIC in externally_consumed


def test_contract_declares_no_state_the_handler_cannot_produce(
    contract: dict[str, Any],
) -> None:
    """Outputs are exactly the two terminal states this COMPUTE node can reach."""
    publish = set(contract["event_bus"]["publish_topics"])
    terminal = set(contract["runtime_dispatch"]["terminal_events"].values())
    assert publish == terminal
