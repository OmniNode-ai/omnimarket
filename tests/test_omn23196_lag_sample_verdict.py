# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23196 AC2: the rule the dev lag samples are judged by."""

from __future__ import annotations

import pytest

from omnimarket.handlers.lag_sample_verdict import lag_samples_meet_bar

BAR = {"min_samples": 60, "max_nonzero_share": 0.5}


def _samples(total: int, nonzero: int) -> list[dict[str, object]]:
    return [
        {
            "at": f"2026-11-08T{i // 60:02d}:{i % 60:02d}:00Z",
            "nonzero_groups": int(i < nonzero),
        }
        for i in range(total)
    ]


def test_fewer_than_half_nonzero_over_enough_samples_meets_the_bar() -> None:
    assert lag_samples_meet_bar(_samples(120, 59), **BAR) is True


def test_exactly_half_nonzero_does_not_meet_the_bar() -> None:
    assert lag_samples_meet_bar(_samples(120, 60), **BAR) is False


def test_the_broken_baseline_does_not_meet_the_bar() -> None:
    # The measured baseline: some group non-zero in 107 of 148 samples.
    assert lag_samples_meet_bar(_samples(148, 107), **BAR) is False


def test_too_few_samples_is_never_a_pass() -> None:
    assert lag_samples_meet_bar(_samples(59, 0), **BAR) is False


def test_a_sample_without_a_whole_number_is_refused_naming_its_position() -> None:
    samples = _samples(60, 0)
    samples[7] = {"at": "2026-11-08T00:07:00Z", "nonzero_groups": "0"}
    with pytest.raises(ValueError, match="sample 7"):
        lag_samples_meet_bar(samples, **BAR)
