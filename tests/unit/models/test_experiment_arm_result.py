# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for ModelExperimentArmResult."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.models.experiment_result.model_experiment_arm_result import (
    ModelExperimentArmResult,
    experiment_arm_result_id_for,
)

pytestmark = pytest.mark.unit

TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
MEASURED_AT = datetime(2026, 11, 13, tzinfo=UTC)
CORPUS_SHA = "a" * 64
TRUTH_SHA = "b" * 64


def _valid_kwargs() -> dict:
    return {
        "tenant_id": TENANT_ID,
        "experiment_id": "exp-001",
        "arm": "control",
        "model_id": "model-x",
        "engine": "vllm",
        "n": 100,
        "correct": 90,
        "wilson_low": 0.80,
        "wilson_high": 0.95,
        "baseline_accuracy": 0.75,
        "latency_p50_ms": 120.0,
        "latency_p90_ms": 250.0,
        "gpu_seconds": 30.0,
        "corpus_sha256": CORPUS_SHA,
        "truth_sha256": TRUTH_SHA,
        "measured_at": MEASURED_AT,
    }


def _build(**overrides: object) -> ModelExperimentArmResult:
    return ModelExperimentArmResult(**{**_valid_kwargs(), **overrides})


def test_golden_round_trip() -> None:
    result = _build()
    dumped = result.model_dump(mode="json")
    restored = ModelExperimentArmResult.model_validate(dumped)
    assert restored == result


def test_id_deterministic_and_changes_with_fields() -> None:
    base = _build()
    base_id = experiment_arm_result_id_for(base)

    # Deterministic
    assert experiment_arm_result_id_for(base) == base_id

    # Changes when measured_at changes
    assert (
        experiment_arm_result_id_for(
            _build(measured_at=datetime(2026, 11, 14, tzinfo=UTC))
        )
        != base_id
    )

    # Changes when arm changes
    assert experiment_arm_result_id_for(_build(arm="treatment")) != base_id

    # Changes when model_id changes
    assert experiment_arm_result_id_for(_build(model_id="model-y")) != base_id

    # Does NOT change when n changes
    assert experiment_arm_result_id_for(_build(n=200)) == base_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("experiment_id", "EXP-001"),
        ("arm", ""),
        ("n", 0),
        ("correct", -1),
        ("wilson_high", 1.5),
        ("corpus_sha256", "a" * 63),
        ("truth_sha256", "z" * 64),
        ("measured_at", datetime(2026, 11, 13)),
        ("unknown_field", "value"),
    ],
)
def test_validation_errors(field: str, value: object) -> None:
    kwargs = _valid_kwargs()
    kwargs[field] = value
    with pytest.raises(ValidationError):
        ModelExperimentArmResult(**kwargs)


def test_control_cross_field_rules_are_not_checked_here() -> None:
    kwargs = _valid_kwargs()
    kwargs["correct"] = 200  # correct > n
    kwargs["latency_p50_ms"] = 300.0
    kwargs["latency_p90_ms"] = 200.0  # p50 > p90
    result = ModelExperimentArmResult(**kwargs)
    assert result.correct == 200
    assert result.n == 100
    assert result.latency_p50_ms == 300.0
    assert result.latency_p90_ms == 200.0
