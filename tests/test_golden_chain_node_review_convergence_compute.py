# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_review_convergence_compute (OMN-13210 / B1).

Request -> COMPUTE result chain: labeled findings yield per-model F1 metrics.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

import omnimarket.nodes.node_review_convergence_compute as _convergence_node_pkg
from omnimarket.models.model_review_finding import EnumFindingCategory
from omnimarket.nodes.node_review_convergence_compute.handlers.handler_convergence_compute import (
    HandlerConvergenceCompute,
)
from omnimarket.nodes.node_review_convergence_compute.models.model_review_convergence import (
    ModelConvergenceInput,
    ModelConvergenceOutput,
    ModelFindingLabel,
)

_CONTRACT_PATH = Path(_convergence_node_pkg.__file__).parent / "contract.yaml"


@pytest.mark.unit
def test_contract_declares_convergence_terminal_event_topics() -> None:
    """The node's declared output states are the two convergence event topics.

    Covers the contract-declared output states for state-coverage: the success
    and failure terminal events plus the published-topic set.
    """
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    terminal_events = contract["runtime_dispatch"]["terminal_events"]
    assert (
        terminal_events["success"]
        == "onex.evt.omnimarket.review-convergence-reduced.v1"
    )
    assert (
        terminal_events["failure"] == "onex.evt.omnimarket.review-convergence-failed.v1"
    )
    assert set(contract["event_bus"]["publish_topics"]) == {
        "onex.evt.omnimarket.review-convergence-reduced.v1",
        "onex.evt.omnimarket.review-convergence-failed.v1",
    }


@pytest.mark.unit
def test_golden_chain_convergence_perfect_agreement() -> None:
    cid = uuid4()
    result = HandlerConvergenceCompute().handle(
        ModelConvergenceInput(
            correlation_id=cid,
            model_key="qwen3-coder",
            labels=[
                ModelFindingLabel(
                    finding_id=uuid4(),
                    category=EnumFindingCategory.SECURITY,
                    local_detected=True,
                    frontier_detected=True,
                ),
                ModelFindingLabel(
                    finding_id=uuid4(),
                    category=EnumFindingCategory.LOGIC_ERROR,
                    local_detected=True,
                    frontier_detected=True,
                ),
            ],
        )
    )
    assert isinstance(result, ModelConvergenceOutput)
    assert result.overall_f1 == 1.0
    assert result.true_positives == 2


@pytest.mark.unit
def test_golden_chain_convergence_empty_labels() -> None:
    result = HandlerConvergenceCompute().handle(
        ModelConvergenceInput(
            correlation_id=uuid4(), model_key="qwen3-coder", labels=[]
        )
    )
    assert isinstance(result, ModelConvergenceOutput)
    assert result.overall_f1 == 0.0
    assert result.total_labels == 0
