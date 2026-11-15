# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The decision rules a to e, their fixed order, and the expiry rule's hold on EXPIRE.

The handler is pure, so every test here builds its input by hand, calls
``handle`` once and reads the action off. The golden case is EXECUTE on a safe
request; each other rule takes one fact away from safe and names the action the
contract lands on. The order tests cross two rules on one input, because the
first that applies wins. The last test is a failing control: with the expiry
rule removed from a copy of the handler, the expired request reads EXECUTE, so
the real handler's EXPIRE is shown to depend on the rule and not on the input.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from omnimarket.models.install_request import (
    EnumInstallRequestAction,
    EnumInstallRequestStatus,
    EnumInstallTarget,
    ModelInstallRequest,
    ModelInstallRequestDecisionInput,
    ModelInstallRequestResult,
)
from omnimarket.nodes.node_install_request_decision_compute.handlers.handler_install_request_decision import (
    HandlerInstallRequestDecision,
)

REQUEST_ID = UUID("5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e01")
OTHER_REQUEST_ID = UUID("5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e02")
REQUESTED_AT = datetime(2026, 11, 15, 1, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
PAST_EXPIRY_NOW = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)


def _request(
    *,
    request_id: UUID = REQUEST_ID,
    host: str = "example-host",
    expires_at: datetime = EXPIRES_AT,
) -> ModelInstallRequest:
    """One install request on the example host, expiring at ``EXPIRES_AT``."""
    return ModelInstallRequest(
        lane="example-lane",
        host=host,
        kind=EnumInstallTarget.UNIT_TREE,
        repository="OmniNode-ai/omnimarket",
        ref="omn-19998-example",
        unit="example-unit",
        verify_command=("true",),
        expected_line="ok",
        request_id=request_id,
        requested_at=REQUESTED_AT,
        expires_at=expires_at,
    )


def _result(
    *,
    request_id: UUID = REQUEST_ID,
    status: EnumInstallRequestStatus = EnumInstallRequestStatus.VERIFIED,
    reason: str = "ok",
) -> ModelInstallRequestResult:
    """One result as the installer on the host would have published it."""
    return ModelInstallRequestResult(
        request_id=request_id,
        host="example-host",
        unit="example-unit",
        status=status,
        at=NOW,
        verify_line="ok" if status is EnumInstallRequestStatus.VERIFIED else "",
        reason=reason,
    )


def _decision_input(
    *,
    request: ModelInstallRequest | None = None,
    executor_host: str = "example-host",
    now: datetime = NOW,
    prior_result: ModelInstallRequestResult | None = None,
    blockers: tuple[str, ...] = (),
) -> ModelInstallRequestDecisionInput:
    """The facts the installer reads, defaulted to the safe golden case."""
    return ModelInstallRequestDecisionInput(
        request=request if request is not None else _request(),
        executor_host=executor_host,
        now=now,
        prior_result=prior_result,
        blockers=blockers,
    )


@pytest.mark.unit
def test_rule_e_safe_request_is_executed() -> None:
    """Golden: a request for this host, unexpired, unblocked and never run executes."""
    decision = HandlerInstallRequestDecision().handle(_decision_input())
    assert decision.request_id == REQUEST_ID
    assert decision.action is EnumInstallRequestAction.EXECUTE
    assert decision.reason == "safe"
    assert decision.detail == ""


@pytest.mark.unit
def test_rule_a_request_for_another_host_is_ignored() -> None:
    """Rule a: the request names another host, so this installer ignores it."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(executor_host="other-host")
    )
    assert decision.action is EnumInstallRequestAction.IGNORE
    assert decision.reason == "other_host"


@pytest.mark.unit
def test_rule_b_verified_prior_result_skips_done() -> None:
    """Rule b: a verified result under the request's own id means it is done."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(prior_result=_result())
    )
    assert decision.action is EnumInstallRequestAction.SKIP_DONE
    assert decision.reason == "already_verified"


@pytest.mark.unit
def test_rule_c_request_past_its_expiry_is_expired() -> None:
    """Rule c: the clock stands past the expiry, so the request expires."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(now=PAST_EXPIRY_NOW)
    )
    assert decision.action is EnumInstallRequestAction.EXPIRE
    assert decision.reason == "expired"


@pytest.mark.unit
def test_rule_d_blocked_request_holds_with_the_blockers_as_detail() -> None:
    """Rule d: a lock held and a unit mid-run hold the request, joined in order."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(blockers=("install-lock-held", "unit-mid-run"))
    )
    assert decision.action is EnumInstallRequestAction.HOLD
    assert decision.reason == "blocked"
    assert decision.detail == "install-lock-held; unit-mid-run"


@pytest.mark.unit
def test_expired_request_with_blockers_expires_before_it_holds() -> None:
    """Order: the expiry rule sits above the blocker rule, so EXPIRE wins."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(now=PAST_EXPIRY_NOW, blockers=("install-lock-held",))
    )
    assert decision.action is EnumInstallRequestAction.EXPIRE
    assert decision.reason == "expired"


@pytest.mark.unit
def test_verified_prior_result_wins_over_expiry() -> None:
    """Order: the skip rule sits above the expiry rule, so SKIP_DONE wins."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(now=PAST_EXPIRY_NOW, prior_result=_result())
    )
    assert decision.action is EnumInstallRequestAction.SKIP_DONE
    assert decision.reason == "already_verified"


@pytest.mark.unit
def test_prior_result_for_another_request_does_not_skip() -> None:
    """The idempotency key is the request id: another id's result does not skip."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(prior_result=_result(request_id=OTHER_REQUEST_ID))
    )
    assert decision.action is EnumInstallRequestAction.EXECUTE
    assert decision.reason == "safe"


@pytest.mark.unit
def test_unverified_prior_result_does_not_skip() -> None:
    """Only ``verified`` clears a request; ``verify_failed`` leaves it to run."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(
            prior_result=_result(
                status=EnumInstallRequestStatus.VERIFY_FAILED,
                reason="verify_mismatch",
            )
        )
    )
    assert decision.action is EnumInstallRequestAction.EXECUTE
    assert decision.reason == "safe"


@pytest.mark.unit
def test_now_exactly_at_the_expiry_is_expired() -> None:
    """Boundary: ``now == expires_at`` stands at the expiry, so it expires."""
    decision = HandlerInstallRequestDecision().handle(_decision_input(now=EXPIRES_AT))
    assert decision.action is EnumInstallRequestAction.EXPIRE
    assert decision.reason == "expired"


class _DecisionWithoutExpiryRule(HandlerInstallRequestDecision):
    """A copy of the real handler with the expiry rule (rule c) removed."""

    def _expired(self, request: ModelInstallRequestDecisionInput) -> bool:
        """The rule under test is gone; the rest of the order stands."""
        return False


@pytest.mark.unit
def test_without_the_expiry_rule_an_expired_request_reads_execute() -> None:
    """Failing control: the EXPIRE above depends on the rule, not on the input.

    The real handler expires this exact input (rule c's test); with the rule
    removed from a copy of the handler, the same input falls through the order
    to EXECUTE. If this test ever fails, the real handler no longer expires by
    its expiry rule alone.
    """
    decision = _DecisionWithoutExpiryRule().handle(_decision_input(now=PAST_EXPIRY_NOW))
    assert decision.action is EnumInstallRequestAction.EXECUTE
    assert decision.reason == "safe"
