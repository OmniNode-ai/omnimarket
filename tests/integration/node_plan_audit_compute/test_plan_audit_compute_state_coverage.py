# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared state-coverage regression tests for node_plan_audit_compute.

OMN-13674 / OMN-13682 (WS-5 Wave 8) under the strengthened full
declared-state-coverage DoD and the AST-hardened state-coverage gate
(OMN-13816). Pins this node's contract-declared output states — the
publish topics the runtime auto-emits and the output-class fields the
projection consumes — to their literal declared values. A silent
contract rename or removal of any declared state now fails here instead
of only surfacing at a live runtime/projection boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from omnimarket.nodes.node_plan_audit_compute.handlers.handler_plan_audit_compute import (
    HandlerPlanAuditCompute,
)
from omnimarket.nodes.node_plan_audit_compute.models.model_plan_audit_compute_request import (
    ModelPlanAuditComputeRequest,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_plan_audit_compute"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CONTRACT_PATH.read_text()))


def test_plan_audit_compute_declares_output_topics() -> None:
    """Every contract-declared publish topic keeps its literal wire string."""
    publish_topics = _load_contract()["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.plan-audit-completed.v1" in publish_topics


def test_plan_audit_compute_emits_declared_output_states(tmp_path: Path) -> None:
    """The handler emits every contract-declared output field (OMN-13923).

    Drives the COMPUTE handler over a real Markdown plan and asserts the
    live result carries the ``verdict``, ``warnings``, and ``plans`` output
    states the contract declares — a non-vacuous state-coverage pin so a
    silent rename/removal of any declared output fails here.
    """
    plan = tmp_path / "rolling-plan.md"
    plan.write_text(
        "# Rolling Plan\n\n"
        "Tracks **OMN-13923** work.\n\n"
        "## Current Verified State\n\n"
        "verified: 2026-07-04 via gh pr checks\n",
        encoding="utf-8",
    )

    result = HandlerPlanAuditCompute().handle(
        ModelPlanAuditComputeRequest(plan_path=str(plan))
    )

    declared_outputs = set(_load_contract()["outputs"])
    assert {"verdict", "warnings", "plans", "passed", "status"} <= declared_outputs

    # Non-vacuous attribute access proves each declared output state is emitted.
    assert result.verdict.value in {"PASS", "WARN", "FAIL", "SKIPPED", "ERROR"}
    assert isinstance(result.warnings, list)
    assert isinstance(result.plans, list)
    assert len(result.plans) == 1
    assert result.plans[0].verdict is result.verdict
    assert result.status == "ok"
    assert result.passed is (result.verdict.value == "PASS")
