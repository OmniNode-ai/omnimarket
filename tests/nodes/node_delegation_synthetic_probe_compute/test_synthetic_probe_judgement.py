# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judgement edges of node_delegation_synthetic_probe_compute (OMN-22977).

A failed terminal that carries answer-like text, a non-finite cost, a deadline missed by a
fraction of a second and an interval that is out of order or mixes naive and aware stamps must
each be refused, never judged a pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_delegation_synthetic_probe_compute.handlers.handler_delegation_synthetic_probe import (
    HandlerDelegationSyntheticProbe,
)
from omnimarket.nodes.node_delegation_synthetic_probe_compute.models.model_delegation_synthetic_probe import (
    EnumSyntheticProbeReason,
    EnumSyntheticProbeVerdict,
    ModelSyntheticProbeRequest,
)

pytestmark = pytest.mark.unit

PROBE = HandlerDelegationSyntheticProbe()
RULES = PROBE.rules
ISSUED = datetime(2026, 11, 7, 9, 5, tzinfo=UTC)
SUCCESS_TERMINAL = "ModelDelegationCompleted"


def request(**overrides: object) -> ModelSyntheticProbeRequest:
    fields: dict[str, object] = {
        "correlation_id": "cid",
        "task_id": "synthetic-edge",
        "issued_at": ISSUED,
        "observed_at": ISSUED + timedelta(seconds=12),
        "terminal_event_type": SUCCESS_TERMINAL,
        "result_text": "pong",
        "cost_usd": 0.0012,
    }
    fields.update(overrides)
    return ModelSyntheticProbeRequest.model_validate(fields)


def test_the_baseline_request_passes() -> None:
    result = PROBE.handle(request())
    assert result.verdict is EnumSyntheticProbeVerdict.PASS
    assert result.reason is EnumSyntheticProbeReason.OK


def test_a_failed_terminal_with_the_answer_is_not_a_pass() -> None:
    result = PROBE.handle(request(terminal_event_type="ModelDelegationFailed"))
    assert result.verdict is EnumSyntheticProbeVerdict.FAIL
    assert result.reason is EnumSyntheticProbeReason.NOT_SUCCESS_TERMINAL
    assert result.failure_reason == "not_success_terminal"
    assert result.row_cells["verdict"] == "FAIL"


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_cost_is_a_missing_cost(cost: float) -> None:
    result = PROBE.handle(request(cost_usd=cost))
    assert result.verdict is EnumSyntheticProbeVerdict.FAIL
    assert result.reason is EnumSyntheticProbeReason.COST_MISSING
    assert "nan" not in result.row_text
    assert "inf" not in result.row_text


def test_a_terminal_a_fraction_over_the_bound_is_over_the_bound() -> None:
    over = request(
        observed_at=ISSUED + timedelta(seconds=RULES.bound_seconds, milliseconds=900)
    )
    result = PROBE.handle(over)
    assert result.verdict is EnumSyntheticProbeVerdict.FAIL
    assert result.reason is EnumSyntheticProbeReason.OVER_BOUND
    assert result.elapsed_seconds == RULES.bound_seconds


def test_a_terminal_exactly_on_the_bound_still_passes() -> None:
    on_bound = request(observed_at=ISSUED + timedelta(seconds=RULES.bound_seconds))
    assert PROBE.handle(on_bound).reason is EnumSyntheticProbeReason.OK


def test_an_observation_before_the_issue_is_refused() -> None:
    with pytest.raises(ValidationError):
        request(observed_at=ISSUED - timedelta(seconds=1))


@pytest.mark.parametrize(
    ("issued_at", "observed_at"),
    [
        (ISSUED, datetime(2026, 11, 7, 9, 5, 12)),
        (datetime(2026, 11, 7, 9, 5), ISSUED + timedelta(seconds=12)),
    ],
)
def test_naive_and_aware_stamps_are_refused_not_a_type_error(
    issued_at: datetime, observed_at: datetime
) -> None:
    with pytest.raises(ValidationError):
        request(issued_at=issued_at, observed_at=observed_at)


def test_two_naive_stamps_in_order_are_judged() -> None:
    naive = datetime(2026, 11, 7, 9, 5)
    result = PROBE.handle(
        request(issued_at=naive, observed_at=naive + timedelta(seconds=12))
    )
    assert result.reason is EnumSyntheticProbeReason.OK
