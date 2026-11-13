# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerChangedPathLive."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_changed_path_live import (
    HandlerChangedPathLive,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_changed_path_live import (
    ModelChangedPathLiveRequest,
    ModelSubstituteReceipt,
)
from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
    ModelLabProofJudgeOverlay,
)

HEAD = "a" * 40


@pytest.fixture
def overlay() -> ModelLabProofJudgeOverlay:
    from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
        load_example_overlay,
    )

    return load_example_overlay()


@pytest.fixture
def handler(overlay: ModelLabProofJudgeOverlay) -> HandlerChangedPathLive:
    return HandlerChangedPathLive(overlay)


def _receipt(
    node: str, head: str = HEAD, exit_code: int = 0, host_class: str = "lab"
) -> ModelSubstituteReceipt:
    return ModelSubstituteReceipt(
        receipt_id=f"{node}-{head[:12]}-labhost",
        repo="OmniNode-ai/omnimarket",
        node=node,
        head=head,
        host="labhost",
        host_class=host_class,
        command="pytest",
        exit_code=exit_code,
        finished_at="2026-01-01T00:00:00+00:00",
        log_path="/tmp/log.txt",
    )


def _request(
    changed_paths: tuple[str, ...],
    contract_local_nodes: tuple[str, ...] = ("node_overlay_merge_order_compute",),
    base_changed_path_live: bool = False,
    substitute_receipts: tuple[ModelSubstituteReceipt, ...] = (),
) -> ModelChangedPathLiveRequest:
    return ModelChangedPathLiveRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=42,
        head=HEAD,
        changed_paths=changed_paths,
        base_changed_path_live=base_changed_path_live,
        contract_local_nodes=contract_local_nodes,
        substitute_receipts=substitute_receipts,
    )


def test_measured_case_not_applicable(handler: HandlerChangedPathLive) -> None:
    """(a) THE MEASURED CASE: matching substitute receipt -> NOT_APPLICABLE."""
    req = _request(
        changed_paths=(
            "src/omnimarket/nodes/node_overlay_drift_effect/models/model_overlay_drift.py",
            "src/omnimarket/nodes/node_overlay_merge_order_compute/handlers/handler_overlay_merge_order.py",
            "src/omnimarket/enums/enum_overlay_verdict.py",
            "tests/unit/nodes/node_overlay_merge_order_compute/test_x.py",
        ),
        substitute_receipts=(_receipt("node_overlay_drift_effect"),),
    )
    result = handler.handle(req)
    assert result.verdict.value == "NOT_APPLICABLE"
    assert result.ok is True
    assert result.class_id == "runs_outside_runtime"
    assert result.use is not None
    assert result.use.outcome.value == "not_applicable"
    assert any(e.node == "node_overlay_drift_effect" for e in result.matched_entries)
    assert len(result.substitute_proofs) == 1


def test_no_substitute_receipt_fails(handler: HandlerChangedPathLive) -> None:
    """(b) Same change with no substitute receipt -> FAIL, refused_no_substitute."""
    req = _request(
        changed_paths=(
            "src/omnimarket/nodes/node_overlay_drift_effect/models/model_overlay_drift.py",
            "src/omnimarket/nodes/node_overlay_merge_order_compute/handlers/handler_overlay_merge_order.py",
        ),
        substitute_receipts=(),
    )
    result = handler.handle(req)
    assert result.verdict.value == "FAIL"
    assert result.ok is False
    assert "node_overlay_drift_effect" in result.missing_proof_nodes
    assert result.use is not None
    assert result.use.outcome.value == "refused_no_substitute"


def test_different_head_fails(handler: HandlerChangedPathLive) -> None:
    """(c) A receipt for a DIFFERENT head -> FAIL."""
    req = _request(
        changed_paths=(
            "src/omnimarket/nodes/node_overlay_drift_effect/models/model_overlay_drift.py",
        ),
        substitute_receipts=(_receipt("node_overlay_drift_effect", head="b" * 40),),
    )
    result = handler.handle(req)
    assert result.verdict.value == "FAIL"
    assert result.ok is False


def test_exit_code_1_fails(handler: HandlerChangedPathLive) -> None:
    """(d) A receipt with exit_code 1 -> FAIL."""
    req = _request(
        changed_paths=(
            "src/omnimarket/nodes/node_overlay_drift_effect/models/model_overlay_drift.py",
        ),
        substitute_receipts=(_receipt("node_overlay_drift_effect", exit_code=1),),
    )
    result = handler.handle(req)
    assert result.verdict.value == "FAIL"
    assert result.ok is False


def test_unaccepted_host_class_fails(handler: HandlerChangedPathLive) -> None:
    """(e) A receipt whose host_class is not accepted -> FAIL."""
    req = _request(
        changed_paths=(
            "src/omnimarket/nodes/node_overlay_drift_effect/models/model_overlay_drift.py",
        ),
        substitute_receipts=(
            _receipt("node_overlay_drift_effect", host_class="runtime"),
        ),
    )
    result = handler.handle(req)
    assert result.verdict.value == "FAIL"
    assert result.ok is False


def test_unmatched_node_fails(handler: HandlerChangedPathLive) -> None:
    """(f) A change that touches a runtime node not in the declaration -> FAIL, unmatched_nodes."""
    req = _request(
        changed_paths=("src/omnimarket/nodes/node_projection_savings/contract.yaml",),
    )
    result = handler.handle(req)
    assert result.verdict.value == "FAIL"
    assert result.ok is False
    assert "node_projection_savings" in result.unmatched_nodes
    assert result.use is None


def test_base_pass_unchanged(handler: HandlerChangedPathLive) -> None:
    """(g) base_changed_path_live True -> PASS unchanged, no use."""
    req = _request(
        changed_paths=(
            "src/omnimarket/nodes/node_overlay_drift_effect/models/model_overlay_drift.py",
        ),
        base_changed_path_live=True,
    )
    result = handler.handle(req)
    assert result.verdict.value == "PASS"
    assert result.ok is True
    assert result.use is None


def test_no_changed_runtime_node_fails(handler: HandlerChangedPathLive) -> None:
    """(h) No changed runtime node -> FAIL."""
    req = _request(
        changed_paths=("src/omnimarket/enums/enum_overlay_verdict.py",),
    )
    result = handler.handle(req)
    assert result.verdict.value == "FAIL"
    assert result.ok is False


def test_non_runtime_path_not_affecting(handler: HandlerChangedPathLive) -> None:
    """(i) A path under .../nodes/<declared node>/tests/ is not runtime-affecting."""
    req = _request(
        changed_paths=(
            "src/omnimarket/nodes/node_overlay_drift_effect/tests/test_x.py",
        ),
    )
    result = handler.handle(req)
    assert result.verdict.value == "FAIL"
    assert result.ok is False
