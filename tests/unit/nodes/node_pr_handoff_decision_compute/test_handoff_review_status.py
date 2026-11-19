# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""pr-handoff's decision refuses a new-work PR whose live head has no success fable-review-gate status.

Each refusal names its case: missing, failing, or on another head (a status on an older head does not count).
A head with a success status is admitted, and a PR that is not new work, or whose statuses were not supplied, is
decided as before. Every refusal has its control: the same request with a success status on the exact head is ready.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.models.model_review_status import ModelReviewStatusRow
from omnimarket.models.pr_handoff import (
    EnumPrHandoffErrorCode,
    EnumPrHandoffVerdict,
    ModelPrHandoffDecision,
    ModelPrHandoffDecisionRequest,
    ModelPrHandoffReviewStatus,
)
from omnimarket.nodes.node_pr_handoff_decision_compute.handlers.handler_pr_handoff_decision import (
    HandlerPrHandoffDecision,
)
from tests.chains.pr_handoff import _builders as b

pytestmark = pytest.mark.unit

CID = UUID("6f0c3a52-8d2b-4a51-9a50-55a1d3f2b0c5")
E = EnumPrHandoffErrorCode
GATE = "fable-review-gate"


def _status(
    head: str, state: str, *, offset: int = 30, context: str = GATE
) -> ModelReviewStatusRow:
    return ModelReviewStatusRow(
        head_sha=head, context=context, state=state, at=b.at(offset)
    )


def _decide(
    review_status: ModelPrHandoffReviewStatus | None,
) -> ModelPrHandoffDecision:
    return HandlerPrHandoffDecision().handle(
        ModelPrHandoffDecisionRequest(
            request=b.request(CID),
            observation=b.observation(b.at(60)).observation(),
            landing_scope=(b.REPO,),
            review_status=review_status,
            now=b.at(60),
        )
    )


def _new_work(*statuses: ModelReviewStatusRow) -> ModelPrHandoffReviewStatus:
    return ModelPrHandoffReviewStatus(new_work=True, statuses=statuses)


def _refused(decision: ModelPrHandoffDecision, code: EnumPrHandoffErrorCode) -> None:
    assert decision.verdict is EnumPrHandoffVerdict.REFUSE
    assert decision.error_code is code
    assert decision.rows is None
    assert decision.live_head_sha == b.HEAD


def test_handoff_review_status_admits_a_success_status_on_the_exact_head() -> None:
    decision = _decide(_new_work(_status(b.HEAD, "success")))
    assert decision.verdict is EnumPrHandoffVerdict.READY
    assert decision.rows is not None


def test_handoff_review_status_refuses_a_new_work_pr_with_no_status_as_missing() -> (
    None
):
    decision = _decide(_new_work())
    _refused(decision, E.REVIEW_STATUS_MISSING)
    assert "no fable-review-gate status on the head" in decision.detail


@pytest.mark.parametrize("state", ["failure", "pending", "error"])
def test_handoff_review_status_refuses_a_status_that_is_not_success_as_failing(
    state: str,
) -> None:
    decision = _decide(_new_work(_status(b.HEAD, state)))
    _refused(decision, E.REVIEW_STATUS_FAILING)
    assert state in decision.detail


def test_handoff_review_status_does_not_count_a_status_on_an_older_head() -> None:
    decision = _decide(_new_work(_status(b.OTHER_HEAD, "success")))
    _refused(decision, E.REVIEW_STATUS_OTHER_HEAD)
    assert "older head does not count" in decision.detail


def test_handoff_review_status_newest_status_on_the_head_decides() -> None:
    flipped_red = _decide(
        _new_work(
            _status(b.HEAD, "success", offset=10), _status(b.HEAD, "failure", offset=20)
        )
    )
    _refused(flipped_red, E.REVIEW_STATUS_FAILING)
    flipped_green = _decide(
        _new_work(
            _status(b.HEAD, "failure", offset=10), _status(b.HEAD, "success", offset=20)
        )
    )
    assert flipped_green.verdict is EnumPrHandoffVerdict.READY


def test_handoff_review_status_ignores_another_context() -> None:
    decision = _decide(_new_work(_status(b.HEAD, "success", context="ci/other")))
    _refused(decision, E.REVIEW_STATUS_MISSING)


def test_handoff_review_status_does_not_gate_a_pr_that_is_not_new_work() -> None:
    decision = _decide(ModelPrHandoffReviewStatus(new_work=False, statuses=()))
    assert decision.verdict is EnumPrHandoffVerdict.READY


def test_handoff_review_status_absent_is_decided_as_before() -> None:
    assert _decide(None).verdict is EnumPrHandoffVerdict.READY


def test_handoff_review_status_rejects_a_short_sha_status_row() -> None:
    with pytest.raises(ValueError, match="String should match pattern"):
        _status("abc1234", "success")
