# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_verified_dispatch_orchestrator.

Tests the worker/verifier dispatch loop, escalation policy, and verification
bundle structure. All tests are pure Python — no I/O, no external services.

Related:
    - OMN-11220: Verification-First Parallel Worker Dispatch Skill
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_verified_dispatch_orchestrator.handlers.handler_verified_dispatch_orchestrator import (
    HandlerVerifiedDispatchOrchestrator,
)
from omnimarket.nodes.node_verified_dispatch_orchestrator.models.model_dispatch_request import (
    ModelDispatchRequest,
)
from omnimarket.nodes.node_verified_dispatch_orchestrator.models.model_escalation_policy import (
    ModelEscalationPolicy,
)
from omnimarket.nodes.node_verified_dispatch_orchestrator.models.model_verification_bundle import (
    ModelAuthoritativeCheck,
    ModelDetectedMismatch,
    ModelVerificationBundle,
)


def _make_request(**overrides: object) -> ModelDispatchRequest:
    defaults: dict[str, object] = {
        "ticket_id": "OMN-11220",
        "worker_prompt": "implement the feature",
        "max_attempts": 3,
        "cooldown_seconds": 0,
        "escalation_action": "linear_ticket",
        "correlation_id": "corr-test-001",
    }
    defaults.update(overrides)
    return ModelDispatchRequest(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
def test_verified_dispatch_accept() -> None:
    """Default stub probes all pass — decision is accept on first attempt."""
    handler = HandlerVerifiedDispatchOrchestrator()
    result = handler.dispatch(_make_request())

    assert result["decision"] == "accept"
    assert result["attempt_count"] == 1
    assert result["escalated"] is False
    bundle = result["verification_bundle"]
    assert bundle is not None
    assert bundle["decision"] == "accept"


@pytest.mark.unit
def test_verified_dispatch_reject_all_escalates() -> None:
    """When the verifier always rejects, escalation fires after max_attempts."""

    class RejectingHandler(HandlerVerifiedDispatchOrchestrator):
        def _probe_surface(
            self,
            *,
            surface: str,
            worker_claim: str,
            ticket_id: str,
        ) -> tuple[bool, str]:
            return (False, f"surface={surface} rejected")

    handler = RejectingHandler()
    request = _make_request(max_attempts=2, cooldown_seconds=0)
    result = handler.dispatch(request)

    assert result["decision"] == "reject"
    assert result["attempt_count"] == 2
    assert result["escalated"] is True


@pytest.mark.unit
def test_verified_dispatch_accept_on_second_attempt() -> None:
    """Verifier rejects once then accepts — escalation does not fire."""

    class OnceRejectingHandler(HandlerVerifiedDispatchOrchestrator):
        def __init__(self) -> None:
            super().__init__()
            self._call_count = 0

        def _probe_surface(
            self,
            *,
            surface: str,
            worker_claim: str,
            ticket_id: str,
        ) -> tuple[bool, str]:
            # Fail on first verifier call (all surfaces), pass on second.
            if self._call_count < 7:  # 7 = len(_AUTHORITATIVE_SURFACES)
                self._call_count += 1
                return (False, f"surface={surface} rejected attempt 1")
            self._call_count += 1
            return (True, f"surface={surface} accepted attempt 2")

    handler = OnceRejectingHandler()
    result = handler.dispatch(_make_request(max_attempts=3, cooldown_seconds=0))

    assert result["decision"] == "accept"
    assert result["attempt_count"] == 2
    assert result["escalated"] is False


@pytest.mark.unit
def test_escalation_policy_defaults() -> None:
    policy = ModelEscalationPolicy()
    assert policy.max_attempts == 3
    assert policy.cooldown_seconds == 60
    assert policy.escalation_action == "linear_ticket"


@pytest.mark.unit
def test_verification_bundle_frozen() -> None:
    """ModelVerificationBundle is immutable after construction."""
    from datetime import UTC, datetime

    bundle = ModelVerificationBundle(
        worker_run_id="w1",
        verifier_run_id="v1",
        claim="test claim",
        authoritative_checks=[],
        detected_mismatches=[],
        decision="accept",
        evidence_refs=[],
        timestamp_utc=datetime.now(UTC),
        correlation_id="corr-001",
    )
    with pytest.raises((TypeError, ValidationError)):
        bundle.decision = "reject"  # type: ignore[misc]


@pytest.mark.unit
def test_dispatch_request_frozen() -> None:
    req = _make_request()
    with pytest.raises((TypeError, ValidationError)):
        req.ticket_id = "OMN-9999"  # type: ignore[misc]


@pytest.mark.unit
def test_model_authoritative_check_structure() -> None:
    check = ModelAuthoritativeCheck(
        surface="github_pr",
        query="probe:github_pr ticket=OMN-11220",
        result="PR #42 checks all green",
        passed=True,
    )
    assert check.surface == "github_pr"
    assert check.passed is True


@pytest.mark.unit
def test_model_detected_mismatch_structure() -> None:
    mismatch = ModelDetectedMismatch(
        surface="ci_checks",
        worker_claim="all CI checks green",
        actual_state="check 'unit-tests' is red",
        severity="critical",
    )
    assert mismatch.severity == "critical"
