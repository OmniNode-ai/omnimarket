# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The decision rules, their fixed order, and the expiry rule's hold on EXPIRE.

The handler is pure, so every test here builds its input by hand, calls
``handle`` once and reads the action off. The golden case is EXECUTE on a safe
request from an allowed publisher; each other rule takes one fact away from
safe and names the action the contract lands on. The order tests cross two
rules on one input, because the first that applies wins. The last tests are
failing controls: with one rule removed from a copy of the handler, the input
the real handler refuses, skips or expires reads EXECUTE, so each real answer
is shown to depend on its rule and not on some other accident.
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
    ModelInstallRequestDecision,
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
OLD_REQUESTED_AT = datetime(2026, 11, 14, 1, 0, tzinfo=UTC)

ALLOWED_PUBLISHERS = ("Example-Lane-Bot",)


def _request(
    *,
    request_id: UUID = REQUEST_ID,
    host: str = "example-host",
    requested_at: datetime = REQUESTED_AT,
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
        verify="example-verify",
        request_id=request_id,
        requested_at=requested_at,
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
    publisher: str = "example-lane-bot",
    allowed_publishers: tuple[str, ...] = ALLOWED_PUBLISHERS,
    max_age_seconds: int = 86400,
) -> ModelInstallRequestDecisionInput:
    """The facts the installer reads, defaulted to the safe golden case."""
    return ModelInstallRequestDecisionInput(
        request=request if request is not None else _request(),
        executor_host=executor_host,
        now=now,
        prior_result=prior_result,
        blockers=blockers,
        publisher=publisher,
        allowed_publishers=allowed_publishers,
        max_age_seconds=max_age_seconds,
    )


@pytest.mark.unit
def test_a_safe_request_from_an_allowed_publisher_is_executed() -> None:
    """Golden: a request for this host, from an allowed login, unexpired, unblocked and never run executes."""
    decision = HandlerInstallRequestDecision().handle(_decision_input())
    assert decision.request_id == REQUEST_ID
    assert decision.action is EnumInstallRequestAction.EXECUTE
    assert decision.reason == "safe"
    assert decision.detail == ""


@pytest.mark.unit
def test_the_publisher_check_folds_case_and_strips_blanks() -> None:
    """The comparison casefolds and strips both sides, so a differently spelled
    allowed login still executes."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(publisher="  EXAMPLE-lane-bot ")
    )
    assert decision.action is EnumInstallRequestAction.EXECUTE
    assert decision.reason == "safe"


@pytest.mark.unit
def test_a_request_for_another_host_is_ignored() -> None:
    """Rule 1: the request names another host, so this installer ignores it."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(executor_host="other-host")
    )
    assert decision.action is EnumInstallRequestAction.IGNORE
    assert decision.reason == "other_host"


@pytest.mark.unit
def test_a_request_from_a_publisher_the_host_does_not_allow_is_refused() -> None:
    """Rule 2: an unlisted login is refused before anything else is read."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(publisher="someone-else")
    )
    assert decision.action is EnumInstallRequestAction.REFUSE
    assert decision.reason == "publisher_not_allowed"


@pytest.mark.unit
def test_a_request_that_names_no_publisher_is_refused() -> None:
    """Rule 2: a request with no one to hold accountable for it is refused."""
    decision = HandlerInstallRequestDecision().handle(_decision_input(publisher=""))
    assert decision.action is EnumInstallRequestAction.REFUSE
    assert decision.reason == "publisher_not_allowed"


@pytest.mark.unit
def test_a_request_for_another_host_wins_over_a_disallowed_publisher() -> None:
    """Order: the ignore rule sits above the publisher rule, so IGNORE wins."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(executor_host="other-host", publisher="someone-else")
    )
    assert decision.action is EnumInstallRequestAction.IGNORE
    assert decision.reason == "other_host"


@pytest.mark.unit
def test_a_disallowed_publisher_wins_over_a_prior_result() -> None:
    """Order: the publisher rule sits above the skip rule, so REFUSE wins."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(publisher="someone-else", prior_result=_result())
    )
    assert decision.action is EnumInstallRequestAction.REFUSE
    assert decision.reason == "publisher_not_allowed"


@pytest.mark.unit
def test_a_verified_prior_result_skips_done() -> None:
    """Rule 3: a verified result under the request's own id means it is done."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(prior_result=_result())
    )
    assert decision.action is EnumInstallRequestAction.SKIP_DONE
    assert decision.reason == "already_verified"


@pytest.mark.unit
def test_a_failed_prior_result_skips_done_too() -> None:
    """Rule 3: any result under the request's own id skips -- a failed request
    is never re-run on redelivery."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(
            prior_result=_result(
                status=EnumInstallRequestStatus.VERIFY_FAILED,
                reason="verify_mismatch",
            )
        )
    )
    assert decision.action is EnumInstallRequestAction.SKIP_DONE
    assert decision.reason == "already_answered"


@pytest.mark.unit
def test_a_prior_result_wins_over_expiry() -> None:
    """Order: the skip rule sits above the expiry rule, so SKIP_DONE wins."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(now=PAST_EXPIRY_NOW, prior_result=_result())
    )
    assert decision.action is EnumInstallRequestAction.SKIP_DONE
    assert decision.reason == "already_verified"


@pytest.mark.unit
def test_a_prior_result_for_another_request_does_not_skip() -> None:
    """The idempotency key is the request id: another id's result does not skip."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(prior_result=_result(request_id=OTHER_REQUEST_ID))
    )
    assert decision.action is EnumInstallRequestAction.EXECUTE
    assert decision.reason == "safe"


@pytest.mark.unit
def test_a_request_past_its_expiry_is_expired() -> None:
    """Rule 4: the clock stands past the expiry, so the request expires."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(now=PAST_EXPIRY_NOW)
    )
    assert decision.action is EnumInstallRequestAction.EXPIRE
    assert decision.reason == "expired"


@pytest.mark.unit
def test_now_exactly_at_the_expiry_is_expired() -> None:
    """Boundary: ``now == expires_at`` stands at the expiry, so it expires."""
    decision = HandlerInstallRequestDecision().handle(_decision_input(now=EXPIRES_AT))
    assert decision.action is EnumInstallRequestAction.EXPIRE
    assert decision.reason == "expired"


@pytest.mark.unit
def test_a_request_older_than_the_command_age_is_expired_inside_its_expiry() -> None:
    """Rule 4: the overlay's command age is the shorter bound -- a day-old
    request is expired though its own expiry is still ahead of the clock."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(
            request=_request(requested_at=OLD_REQUESTED_AT), max_age_seconds=21600
        )
    )
    assert decision.action is EnumInstallRequestAction.EXPIRE
    assert decision.reason == "expired"


@pytest.mark.unit
def test_now_exactly_at_the_age_limit_is_expired() -> None:
    """Boundary: ``now == requested_at + max_age_seconds`` stands at the limit."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(
            now=datetime(2026, 11, 15, 7, 0, tzinfo=UTC), max_age_seconds=21600
        )
    )
    assert decision.action is EnumInstallRequestAction.EXPIRE
    assert decision.reason == "expired"


@pytest.mark.unit
def test_an_expired_request_expires_before_it_holds() -> None:
    """Order: the expiry rule sits above the blocker rule, so EXPIRE wins."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(now=PAST_EXPIRY_NOW, blockers=("install-lock-held",))
    )
    assert decision.action is EnumInstallRequestAction.EXPIRE
    assert decision.reason == "expired"


@pytest.mark.unit
def test_a_blocked_request_holds_with_the_blockers_as_detail() -> None:
    """Rule 5: a lock held and a unit mid-run hold the request, joined in order."""
    decision = HandlerInstallRequestDecision().handle(
        _decision_input(blockers=("install-lock-held", "unit-mid-run"))
    )
    assert decision.action is EnumInstallRequestAction.HOLD
    assert decision.reason == "blocked"
    assert decision.detail == "install-lock-held; unit-mid-run"


class _DecisionWithoutThePublisherRule(HandlerInstallRequestDecision):
    """A copy of the real handler with the publisher rule (rule 2) removed."""

    def _publisher_allowed(self, request: ModelInstallRequestDecisionInput) -> bool:
        return True


@pytest.mark.unit
def test_without_the_publisher_rule_a_disallowed_publisher_reads_execute() -> None:
    """Failing control: the REFUSE above depends on the rule, not on the input."""
    decision = _DecisionWithoutThePublisherRule().handle(
        _decision_input(publisher="someone-else")
    )
    assert decision.action is EnumInstallRequestAction.EXECUTE
    assert decision.reason == "safe"


class _DecisionWithoutThePriorResultRule(HandlerInstallRequestDecision):
    """A copy of the real handler that never sees a prior result (rule 3 gone)."""

    def handle(
        self, request: ModelInstallRequestDecisionInput
    ) -> ModelInstallRequestDecision:
        return super().handle(request.model_copy(update={"prior_result": None}))


@pytest.mark.unit
def test_without_the_prior_result_rule_a_failed_request_reads_execute() -> None:
    """Failing control: the SKIP_DONE of a failed request depends on the rule.

    The real handler skips this exact input (the failed-prior test); with the
    rule removed from a copy of the handler, the same input falls through the
    order to EXECUTE -- which would re-run a failed request on redelivery.
    """
    decision = _DecisionWithoutThePriorResultRule().handle(
        _decision_input(
            prior_result=_result(
                status=EnumInstallRequestStatus.VERIFY_FAILED,
                reason="verify_mismatch",
            )
        )
    )
    assert decision.action is EnumInstallRequestAction.EXECUTE
    assert decision.reason == "safe"


class _DecisionWithoutTheExpiryRule(HandlerInstallRequestDecision):
    """A copy of the real handler with the expiry rule (rule 4) removed."""

    def _expired(self, request: ModelInstallRequestDecisionInput) -> bool:
        return False


@pytest.mark.unit
def test_without_the_expiry_rule_an_expired_request_reads_execute() -> None:
    """Failing control: the EXPIRE above depends on the rule, not on the input.

    The real handler expires this exact input (the expiry test); with the rule
    removed from a copy of the handler, the same input falls through the order
    to EXECUTE.
    """
    decision = _DecisionWithoutTheExpiryRule().handle(
        _decision_input(now=PAST_EXPIRY_NOW)
    )
    assert decision.action is EnumInstallRequestAction.EXECUTE
    assert decision.reason == "safe"
