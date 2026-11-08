# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Three review findings on the frozen andon threshold model (OMN-22942).

1. ``frozen=True`` does not make a ``dict`` field immutable, and the hash is checked only at
   construction, so a value changed after the freeze was read changed the burst decision
   with no hash check. The frozen thresholds must refuse mutation.
2. ``window_hours`` was any positive int while burst scope compares a threshold with a
   24 hour count, so a freeze or request for another window was judged against the wrong
   counts. Only the 24 hour window is accepted.
3. A negative threshold with a matching hash validated. A threshold is a count: never
   negative.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from omnimarket.models.model_andon_threshold import (
    WINDOW_HOURS,
    ModelAndonThresholdFreeze,
    ModelClassWindowCounts,
    ModelThresholdComputeRequest,
    freeze_hash,
)

pytestmark = pytest.mark.unit

RECALIBRATED = date(2026, 11, 7)


def _freeze(**fields: object) -> ModelAndonThresholdFreeze:
    thresholds = {"ci.timeout": 2, "hook.refusal": 17}
    window = int(fields.pop("window_hours", WINDOW_HOURS))  # type: ignore[call-overload]
    values = dict(fields.pop("thresholds", thresholds))  # type: ignore[call-overload]
    return ModelAndonThresholdFreeze(
        window_hours=window,
        thresholds=values,
        threshold_hash=freeze_hash(window, values),
        recalibrated_on=RECALIBRATED,
    )


def test_a_threshold_value_cannot_be_changed_after_the_freeze_is_built() -> None:
    freeze = _freeze()
    with pytest.raises(TypeError):
        freeze.thresholds["hook.refusal"] = 10_000  # type: ignore[index]
    assert freeze.thresholds["hook.refusal"] == 17


def test_the_freeze_hash_still_matches_after_a_refused_change() -> None:
    freeze = _freeze()
    with pytest.raises(TypeError):
        freeze.thresholds["new.class"] = 1  # type: ignore[index]
    assert freeze.threshold_hash == freeze_hash(
        freeze.window_hours, dict(freeze.thresholds)
    )


def test_a_freeze_for_another_window_is_refused() -> None:
    with pytest.raises(ValidationError):
        _freeze(window_hours=1)


def test_a_compute_request_for_another_window_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelThresholdComputeRequest(
            classes=(ModelClassWindowCounts(class_id="a", window_counts=(1,)),),
            recalibrated_on=RECALIBRATED,
            window_hours=48,
        )


def test_the_24_hour_window_is_accepted() -> None:
    assert _freeze(window_hours=24).window_hours == 24


def test_a_negative_threshold_with_a_matching_hash_is_refused() -> None:
    with pytest.raises(ValidationError):
        _freeze(thresholds={"ci.timeout": -1})


def test_a_zero_threshold_is_accepted() -> None:
    assert _freeze(thresholds={"ci.timeout": 0}).thresholds["ci.timeout"] == 0
