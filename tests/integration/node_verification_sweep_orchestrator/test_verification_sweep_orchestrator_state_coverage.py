# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared state-coverage regression tests for
node_verification_sweep_orchestrator.

OMN-14552 (under the full declared-state-coverage DoD and the AST-hardened
state-coverage gate, OMN-13816). Pins this node's contract-declared output
states — the ``outputs`` fields the projection consumes and the publish topics
the runtime auto-emits — to their literal declared values, and asserts each
output field is a real field on the result model. A silent contract rename or
model drift of any declared state now fails here instead of only surfacing at a
live runtime/projection boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_result import (
    ModelVerificationSweepOrchestratorResult,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_verification_sweep_orchestrator"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def test_verification_sweep_declares_output_topics() -> None:
    """Every contract-declared publish topic keeps its literal wire string."""
    publish_topics = _load_contract()["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.verification-sweep-finding.v1" in publish_topics
    assert "onex.evt.omnimarket.verification-sweep-completed.v1" in publish_topics
    assert "onex.evt.omnimarket.sweep-result.v1" in publish_topics


def test_verification_sweep_result_covers_declared_output_fields() -> None:
    """Every contract ``outputs`` field is a real field on the result model."""
    fields = set(ModelVerificationSweepOrchestratorResult.model_fields)
    assert "endpoint_results" in fields
    assert "db_checks" in fields
    assert "dod_receipts" in fields
    assert "scanned_count" in fields
    assert "overall_status" in fields
    assert "receipt_path" in fields
    assert "adapter_errors" in fields


def test_contract_outputs_and_result_model_are_in_sync() -> None:
    """The contract ``outputs`` keys and the result-model fields do not drift."""
    declared_outputs = set(_load_contract()["outputs"])
    model_fields = set(ModelVerificationSweepOrchestratorResult.model_fields)
    # Every documented output must exist on the model (no phantom outputs).
    assert declared_outputs <= model_fields, declared_outputs - model_fields
    # scanned_count is the OMN-14552 census field — it must be documented.
    assert "scanned_count" in declared_outputs
