# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm owner responder's error chain (OMN-24200)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_alarm_owner_responder_compute import (
    HandlerAlarmOwnerResponder,
    ModelAlarmOwnerPolicy,
    ModelAlarmOwnerRequest,
)

pytestmark = pytest.mark.unit

POLICY = ModelAlarmOwnerPolicy(
    alarm_actor="launchd:lab-alarm",
    conditions=("delegation_chain_canary",),
    owner_required_after_hours=6.0,
    redispatch_after_hours=24.0,
)

GOOD_ROW = (
    "2026-11-03T12:52:08Z | FRICTION | lane=lab-alarm | actor=launchd:lab-alarm | "
    "existing=OMN-20769 | cost=... | condition=delegation_chain_canary | "
    "subject=OmniNode-ai/omnibase_infra chain-canary.yml@dev | raised_at=2026-11-03T12:52:08Z | "
    "detail=recorded | evidence=recorded | req=r | lab alarm raised delegation_chain_canary on "
    "OmniNode-ai/omnibase_infra chain-canary.yml@dev; the detail and evidence cells carry what it read"
)


def _request(rows: tuple[str, ...], **kwargs: object) -> ModelAlarmOwnerRequest:
    return ModelAlarmOwnerRequest(
        rows=rows, now="2026-11-13T12:00:00Z", policy=POLICY, **kwargs
    )


def test_a_bad_calendar_date_is_refused_alone_and_the_batch_continues() -> None:
    bad = GOOD_ROW.replace("2026-11-03T12:52:08Z", "2026-11-31T00:00:00Z", 1)
    result = HandlerAlarmOwnerResponder().handle(_request((bad, GOOD_ROW)))
    assert len(result.refusals) == 1
    assert result.refusals[0].stamp == "2026-11-31T00:00:00Z"
    assert result.refusals[0].reason == "invalid-calendar-date"
    assert result.alarm_rows == 1
    assert len(result.dispatches) == 1


def test_a_row_without_a_subject_is_refused() -> None:
    row = GOOD_ROW.replace(
        "subject=OmniNode-ai/omnibase_infra chain-canary.yml@dev | ", ""
    )
    result = HandlerAlarmOwnerResponder().handle(_request((row,)))
    assert len(result.refusals) == 1
    assert result.refusals[0].reason == "missing-subject"
    assert result.alarm_rows == 0


def test_a_row_without_a_condition_is_refused() -> None:
    row = GOOD_ROW.replace("condition=delegation_chain_canary | ", "")
    result = HandlerAlarmOwnerResponder().handle(_request((row,)))
    assert len(result.refusals) == 1
    assert result.refusals[0].reason == "missing-condition"
    assert result.alarm_rows == 0


def test_now_must_be_a_real_calendar_stamp() -> None:
    for bad in ("yesterday", "2026-02-31T00:00:00Z", "2026-11-13 12:00:00"):
        with pytest.raises(ValidationError):
            ModelAlarmOwnerRequest(rows=(GOOD_ROW,), now=bad, policy=POLICY)


def test_owner_required_after_hours_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        ModelAlarmOwnerPolicy(
            alarm_actor="launchd:lab-alarm",
            conditions=("delegation_chain_canary",),
            owner_required_after_hours=0.0,
            redispatch_after_hours=24.0,
        )


def test_a_row_from_a_different_actor_is_ignored() -> None:
    other = GOOD_ROW.replace("actor=launchd:lab-alarm", "actor=launchd:other")
    result = HandlerAlarmOwnerResponder().handle(_request((other,)))
    assert result.alarm_rows == 0
    assert result.dispatches == ()
