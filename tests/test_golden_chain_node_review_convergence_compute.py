# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_review_convergence_compute (OMN-13210 / B1).

Request -> COMPUTE result chain: labeled findings yield per-model F1 metrics.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind

from omnimarket.models.model_review_finding import EnumFindingCategory
from omnimarket.nodes.node_review_convergence_compute.handlers.handler_convergence_compute import (
    HandlerConvergenceCompute,
)
from omnimarket.nodes.node_review_convergence_compute.models.model_review_convergence import (
    ModelConvergenceInput,
    ModelConvergenceOutput,
    ModelFindingLabel,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_convergence_perfect_agreement() -> None:
    cid = uuid4()
    output = await HandlerConvergenceCompute().handle(
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
    assert output.node_kind == EnumNodeKind.COMPUTE
    assert output.correlation_id == cid
    assert isinstance(output.result, ModelConvergenceOutput)
    assert output.result.overall_f1 == 1.0
    assert output.result.true_positives == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_convergence_empty_labels() -> None:
    output = await HandlerConvergenceCompute().handle(
        ModelConvergenceInput(
            correlation_id=uuid4(), model_key="qwen3-coder", labels=[]
        )
    )
    assert isinstance(output.result, ModelConvergenceOutput)
    assert output.result.overall_f1 == 0.0
    assert output.result.total_labels == 0
