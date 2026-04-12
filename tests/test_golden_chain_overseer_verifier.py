# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_overseer_verifier.

Tests the five check dimensions and failure classification priority order.
All tests are pure Python — no I/O, no LLM calls.

Related:
    - OMN-8031: node_overseer_verifier in omnimarket
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier import (
    HandlerOverseerVerifier,
)
from omnimarket.nodes.node_overseer_verifier.models.model_verifier_request import (
    ModelVerifierRequest,
)


def _make_request(**overrides: object) -> ModelVerifierRequest:
    """Build a valid ModelVerifierRequest with sensible defaults."""
    defaults: dict[str, object] = {
        "task_id": "task-abc-123",
        "status": "running",
        "domain": "build",
        "node_id": "node_build_loop",
        "runner_id": "runner-001",
        "attempt": 1,
        "payload": {"key": "value"},
        "error": None,
        "confidence": 0.9,
        "cost_so_far": 0.05,
        "allowed_actions": ["dispatch", "complete"],
        "declared_invariants": [],
        "schema_version": "1.0",
    }
    defaults.update(overrides)
    return ModelVerifierRequest(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. input_completeness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_input_completeness_check_pass() -> None:
    """A valid envelope with all required fields passes input_completeness."""
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request())

    assert result["verdict"] == "PASS"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["input_completeness"]["passed"] is True


@pytest.mark.unit
def test_input_completeness_check_fail_empty_task_id() -> None:
    """An empty task_id fails input_completeness and returns FAIL verdict."""
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request(task_id=""))

    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["input_completeness"]["passed"] is False
    assert "task_id" in checks["input_completeness"]["message"]


@pytest.mark.unit
def test_input_completeness_check_fail_empty_status() -> None:
    """An empty status fails input_completeness."""
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request(status=""))

    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["input_completeness"]["passed"] is False
    assert "status" in checks["input_completeness"]["message"]


@pytest.mark.unit
def test_input_completeness_check_fail_empty_domain() -> None:
    """An empty domain fails input_completeness."""
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request(domain=""))

    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["input_completeness"]["passed"] is False


@pytest.mark.unit
def test_input_completeness_check_fail_empty_node_id() -> None:
    """An empty node_id fails input_completeness."""
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request(node_id=""))

    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["input_completeness"]["passed"] is False
    assert "node_id" in checks["input_completeness"]["message"]


# ---------------------------------------------------------------------------
# 2. invariant_preservation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invariant_check_detects_negative_cost() -> None:
    """cost_so_far=-1.0 triggers FAIL with INVARIANT_VIOLATION escalation.

    Per spec: INVARIANT_VIOLATION maps to ESCALATE verdict via _ESCALATE_REASONS.
    """
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request(cost_so_far=-1.0))

    assert result["verdict"] == "ESCALATE"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["invariant_preservation"]["passed"] is False
    assert "INVARIANT_VIOLATION" in checks["invariant_preservation"]["message"]


@pytest.mark.unit
def test_invariant_check_passes_zero_cost() -> None:
    """cost_so_far=0.0 is a valid invariant (boundary value)."""
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request(cost_so_far=0.0))

    assert result["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# 3. outcome_success_validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_verifier_returns_escalate_on_low_confidence() -> None:
    """confidence=0.1 triggers ESCALATE verdict.

    Low confidence maps to VERIFIER_REJECTION which is in _ESCALATE_REASONS,
    routing to ESCALATE when input_completeness and invariant_preservation are clean.
    """
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request(confidence=0.1))

    # VERIFIER_REJECTION is in _ESCALATE_REASONS → ESCALATE
    assert result["verdict"] == "ESCALATE"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["outcome_success_validation"]["passed"] is False


@pytest.mark.unit
def test_outcome_validation_passes_at_threshold() -> None:
    """confidence=0.5 exactly meets the threshold."""
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request(confidence=0.5))

    assert result["verdict"] == "PASS"


@pytest.mark.unit
def test_outcome_validation_skipped_when_confidence_absent() -> None:
    """No confidence provided — check passes (optional field)."""
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request(confidence=None))

    assert result["verdict"] == "PASS"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["outcome_success_validation"]["passed"] is True


# ---------------------------------------------------------------------------
# 4. allowed_action_scope
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_allowed_action_scope_pass() -> None:
    """All actions within permitted scope passes."""
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request(allowed_actions=["dispatch", "complete"]))

    assert result["verdict"] == "PASS"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["allowed_action_scope"]["passed"] is True


@pytest.mark.unit
def test_allowed_action_scope_fails_on_unknown_action() -> None:
    """An unrecognised action name fails allowed_action_scope."""
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request(allowed_actions=["dispatch", "delete_all"]))

    # allowed_action_scope fails but input/invariant pass → check priority
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["allowed_action_scope"]["passed"] is False
    assert "delete_all" in checks["allowed_action_scope"]["message"]


# ---------------------------------------------------------------------------
# 5. contract_compliance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_compliance_fails_empty_schema_version() -> None:
    """An empty schema_version fails contract_compliance."""
    handler = HandlerOverseerVerifier()
    result = handler.verify(_make_request(schema_version=""))

    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["contract_compliance"]["passed"] is False
    assert "schema_version" in checks["contract_compliance"]["message"]


# ---------------------------------------------------------------------------
# 6. _classify_failure priority
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_multiple_failures_input_completeness_wins() -> None:
    """When multiple checks fail, input_completeness has highest priority.

    input_completeness > invariant_preservation > outcome_success_validation
    > allowed_action_scope > contract_compliance
    """
    handler = HandlerOverseerVerifier()
    # Trigger three failures simultaneously: task_id empty (input_completeness),
    # negative cost (invariant_preservation), low confidence (outcome_success)
    result = handler.verify(
        _make_request(
            task_id="",
            cost_so_far=-1.0,
            confidence=0.1,
        )
    )

    # input_completeness is highest priority → FAIL (not ESCALATE)
    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["input_completeness"]["passed"] is False
    assert checks["invariant_preservation"]["passed"] is False
    assert checks["outcome_success_validation"]["passed"] is False
    # Dominant check must be input_completeness
    assert "input_completeness" in result["summary"]  # type: ignore[operator]


@pytest.mark.unit
def test_invariant_wins_over_outcome_when_input_passes() -> None:
    """invariant_preservation (priority 2) dominates outcome_success_validation (priority 3)."""
    handler = HandlerOverseerVerifier()
    result = handler.verify(
        _make_request(
            cost_so_far=-5.0,
            confidence=0.1,
        )
    )

    # Both invariant_preservation and outcome_success_validation fail.
    # invariant_preservation is higher priority → ESCALATE (INVARIANT_VIOLATION)
    assert result["verdict"] == "ESCALATE"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["invariant_preservation"]["passed"] is False


# ---------------------------------------------------------------------------
# 7. invariant_registry (OMN-8505)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invariant_registry_is_queryable_for_model_session_contract() -> None:
    """Invariant registry returns non-empty invariants for model_session_contract."""
    from omnimarket.nodes.node_overseer_verifier.invariant_registry import (
        INVARIANT_REGISTRY,
    )

    invariants = INVARIANT_REGISTRY.get("model_session_contract", [])
    assert len(invariants) > 0, (
        "model_session_contract must have at least one invariant"
    )


@pytest.mark.unit
def test_invariant_registry_queryable_for_build_loop_contract() -> None:
    """Invariant registry returns entries for build_loop_contract."""
    from omnimarket.nodes.node_overseer_verifier.invariant_registry import (
        INVARIANT_REGISTRY,
    )

    invariants = INVARIANT_REGISTRY.get("build_loop_contract", [])
    assert len(invariants) > 0, "build_loop_contract must have at least one invariant"


@pytest.mark.unit
def test_invariant_registry_returns_empty_list_for_unknown_contract() -> None:
    """Unknown contract key returns empty list (graceful default)."""
    from omnimarket.nodes.node_overseer_verifier.invariant_registry import (
        INVARIANT_REGISTRY,
    )

    invariants = INVARIANT_REGISTRY.get("nonexistent_contract_xyz", [])
    assert invariants == []


@pytest.mark.unit
def test_invariant_registry_entries_have_required_fields() -> None:
    """Each invariant entry has 'name' and 'description' fields."""
    from omnimarket.nodes.node_overseer_verifier.invariant_registry import (
        INVARIANT_REGISTRY,
    )

    for contract_key, entries in INVARIANT_REGISTRY.items():
        for entry in entries:
            assert "name" in entry, f"{contract_key}: entry missing 'name'"
            assert "description" in entry, (
                f"{contract_key}: entry missing 'description'"
            )
