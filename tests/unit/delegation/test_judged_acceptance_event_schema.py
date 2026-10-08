# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The offline judged verdict requires calibration and a replay-stable key."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.events import ModelDelegationAcceptanceJudgedEvent
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_failure_class import (
    EnumAcceptanceFailureClass,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def payload() -> dict[str, Any]:
    return {
        "correlation_id": UUID("00000000-0000-4000-8000-000000000001"),
        "tenant_id": UUID("00000000-0000-4000-8000-000000000002"),
        "delegated_model_key": "synthetic-model",
        "delegated_tier": "local",
        "task_type": "test",
        "kind": "task",
        "call_time": datetime(2026, 10, 1, tzinfo=UTC),
        "judge_run_id": "synthetic-judge-run-1",
        "judge_model": "synthetic-judge",
        "judge_model_version": "v1",
        "rubric_id": "synthetic-rubric",
        "rubric_version": "v1",
        "rubric_hash": "sha256:" + "a" * 64,
        "calibration_run_id": "synthetic-calibration-1",
        "calibration_n": 76,
        "calibration_agreement": 0.803,
        "calibration_kappa": 0.61,
        "accept": False,
        "quality": 1,
        "failure_class": EnumAcceptanceFailureClass.BROKEN_CODE,
        "judged_at": datetime(2026, 10, 2, tzinfo=UTC),
    }


def test_judged_acceptance_event_schema_requires_calibration_run_id(
    payload: dict[str, Any],
) -> None:
    del payload["calibration_run_id"]
    with pytest.raises(ValidationError, match="calibration_run_id"):
        ModelDelegationAcceptanceJudgedEvent.model_validate(payload)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_judged_acceptance_event_schema_refuses_empty_calibration_run_id(
    payload: dict[str, Any], value: object
) -> None:
    payload["calibration_run_id"] = value
    with pytest.raises(ValidationError, match="calibration_run_id"):
        ModelDelegationAcceptanceJudgedEvent.model_validate(payload)


@pytest.mark.parametrize("quality", [-1, 4, 1.5, True])
def test_judged_acceptance_event_schema_refuses_invalid_quality(
    payload: dict[str, Any], quality: object
) -> None:
    payload["quality"] = quality
    with pytest.raises(ValidationError, match="quality"):
        ModelDelegationAcceptanceJudgedEvent.model_validate(payload)


@pytest.mark.parametrize("kind", ["probe", "liveness_probe", "config_swap", ""])
def test_judged_acceptance_event_schema_refuses_other_kinds(
    payload: dict[str, Any], kind: str
) -> None:
    payload["kind"] = kind
    with pytest.raises(ValidationError, match="kind"):
        ModelDelegationAcceptanceJudgedEvent.model_validate(payload)


@pytest.mark.parametrize("kind", ["task", "edit_loop_turn"])
@pytest.mark.parametrize("quality", [0, 1, 2, 3])
def test_judged_acceptance_event_schema_preserves_verdict_and_provenance(
    payload: dict[str, Any], kind: str, quality: int
) -> None:
    payload.update(kind=kind, quality=quality)
    event = ModelDelegationAcceptanceJudgedEvent.model_validate(payload)
    assert event.model_dump(exclude={"event_id"}) == payload
    assert event.failure_class is EnumAcceptanceFailureClass.BROKEN_CODE
    assert (
        ModelDelegationAcceptanceJudgedEvent.model_validate_json(
            event.model_dump_json()
        )
        == event
    )


def test_judged_acceptance_event_id_deterministic(payload: dict[str, Any]) -> None:
    first = ModelDelegationAcceptanceJudgedEvent.model_validate(payload)
    replay = ModelDelegationAcceptanceJudgedEvent.model_validate(dict(payload))
    assert first.event_id == replay.event_id
    assert first.event_id.version == 5
    assert first.model_dump_json() == replay.model_dump_json()
    # A changed verdict/time is still the same judged output in the same run.
    payload.update(quality=3, judged_at=datetime(2026, 10, 3, tzinfo=UTC))
    assert ModelDelegationAcceptanceJudgedEvent.model_validate(payload).event_id == (
        first.event_id
    )


def test_judged_acceptance_event_id_deterministic_distinct_pairs(
    payload: dict[str, Any],
) -> None:
    ids = {
        ModelDelegationAcceptanceJudgedEvent.model_validate(
            {**payload, "judge_run_id": run, "correlation_id": UUID(int=call)}
        ).event_id
        for run in ("synthetic-run-a", "synthetic-run-b")
        for call in (1, 2, 3)
    }
    assert len(ids) == 6


def test_judged_acceptance_event_id_deterministic_refuses_forged_id(
    payload: dict[str, Any],
) -> None:
    payload["event_id"] = UUID(int=0)
    with pytest.raises(ValidationError, match="event_id"):
        ModelDelegationAcceptanceJudgedEvent.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("calibration_n", 0),
        ("calibration_agreement", -0.01),
        ("calibration_agreement", 1.01),
        ("calibration_kappa", -1.01),
        ("calibration_kappa", 1.01),
        ("calibration_kappa", float("nan")),
        ("failure_class", "unrecognised"),
        ("call_time", datetime(2026, 10, 1)),
        ("judged_at", datetime(2026, 10, 2)),
    ],
)
def test_judged_acceptance_event_schema_refuses_invalid_provenance(
    payload: dict[str, Any], field: str, value: object
) -> None:
    payload[field] = value
    with pytest.raises(ValidationError, match=field):
        ModelDelegationAcceptanceJudgedEvent.model_validate(payload)


def test_judged_acceptance_event_schema_carries_low_calibration_for_fold_refusal(
    payload: dict[str, Any],
) -> None:
    payload.update(calibration_agreement=0.70, calibration_kappa=-0.10)
    event = ModelDelegationAcceptanceJudgedEvent.model_validate(payload)
    assert event.calibration_agreement == 0.70
    assert event.calibration_kappa == -0.10


def test_judged_acceptance_event_schema_topic() -> None:
    from omnimarket.events.topics import DELEGATION_ACCEPTANCE_JUDGED_TOPIC_V1

    assert DELEGATION_ACCEPTANCE_JUDGED_TOPIC_V1 == (
        "onex.evt.omnimarket.delegation-acceptance-judged.v1"
    )
