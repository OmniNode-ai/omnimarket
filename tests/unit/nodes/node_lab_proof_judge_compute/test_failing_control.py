# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failing control: the judge reads FAIL for the measured case when the substitute receipt list is empty."""

from __future__ import annotations

from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_changed_path_live import (
    HandlerChangedPathLive,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_changed_path_live import (
    ModelChangedPathLiveRequest,
)
from omnimarket.nodes.node_lab_proof_judge_compute.overlay import load_example_overlay

HEAD = "a" * 40


def test_failing_control_empty_receipts() -> None:
    """With no substitute receipts, the measured case must FAIL (not NOT_APPLICABLE)."""
    overlay = load_example_overlay()
    handler = HandlerChangedPathLive(overlay)
    req = ModelChangedPathLiveRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=42,
        head=HEAD,
        changed_paths=(
            "src/omnimarket/nodes/node_overlay_drift_effect/models/model_overlay_drift.py",
            "src/omnimarket/nodes/node_overlay_merge_order_compute/handlers/handler_overlay_merge_order.py",
        ),
        base_changed_path_live=False,
        contract_local_nodes=("node_overlay_merge_order_compute",),
        substitute_receipts=(),
    )
    result = handler.handle(req)
    # This test would not pass if the handler returned NOT_APPLICABLE without a proof.
    assert result.verdict.value == "FAIL"
    assert result.ok is False
