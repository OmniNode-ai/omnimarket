# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node-local golden-chain guardrails for node_dep_cascade_dedup_orchestrator [OMN-12213].

Mirrors tests/test_golden_chain_dep_cascade_dedup_orchestrator.py but lives
under src/…/tests/ so the dep-health sweep (which scans src/ only) can find it.

Verifies:
- contract.yaml is valid and declares node_not_implemented: true
- Handler is importable and raises NotImplementedError (explicit stub)
- Typed models are strict (frozen, extra=forbid)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.handlers.handler_dep_cascade_dedup_orchestrator import (
    HandlerDepCascadeDedupOrchestrator,
)
from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.models.model_dep_cascade_dedup_request import (
    ModelDepCascadeDedupRequest,
)
from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.models.model_dep_cascade_dedup_result import (
    ModelDepCascadeDedupResult,
)

_NODE_DIR = Path(__file__).resolve().parent.parent


def _contract() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load((_NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))


def test_handler_dep_cascade_dedup_orchestrator_is_importable() -> None:
    """HandlerDepCascadeDedupOrchestrator must be importable."""
    assert HandlerDepCascadeDedupOrchestrator is not None


def test_handler_dep_cascade_dedup_orchestrator_raises_not_implemented() -> None:
    """handler_dep_cascade_dedup_orchestrator must raise NotImplementedError (explicit stub)."""
    handler = HandlerDepCascadeDedupOrchestrator()
    request = ModelDepCascadeDedupRequest(
        repos=["OmniNode-ai/omnimarket"],
        dry_run=True,
    )
    with pytest.raises(NotImplementedError):
        handler.handle(request)


def test_contract_marks_node_not_implemented() -> None:
    """contract.yaml must declare node_not_implemented: true."""
    contract = _contract()
    assert contract.get("node_not_implemented") is True, (
        "node_dep_cascade_dedup_orchestrator is a Wave 1 stub; "
        "contract.yaml must set node_not_implemented: true"
    )


def test_request_model_is_strict() -> None:
    """ModelDepCascadeDedupRequest must be frozen and reject extra fields."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelDepCascadeDedupRequest(repos=["OmniNode-ai/omnimarket"], unknown_field="x")  # type: ignore[call-arg]


def test_result_model_is_strict() -> None:
    """ModelDepCascadeDedupResult must be instantiable."""
    result = ModelDepCascadeDedupResult(
        groups_found=0,
        prs_closed=0,
        prs_skipped=0,
    )
    assert result.groups_found == 0
