# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tessl review findings on the publish phase budget: the limit given to a call never passes what the phase has left."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    PhaseBudget,
    PublishRefusedError,
)

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_limit_of_a_call_never_passes_what_the_phase_has_left() -> None:
    clock = _Clock()
    budget = PhaseBudget(clock, 10, "copy", 60)
    clock.now += 0.5  # 9.5 s left
    assert budget.timeout() <= budget.remaining()


def test_a_fraction_of_a_second_left_refuses_instead_of_granting_a_whole_second() -> (
    None
):
    clock = _Clock()
    budget = PhaseBudget(clock, 10, "copy", 60)
    clock.now += 9.9  # 0.1 s left
    with pytest.raises(PublishRefusedError):
        budget.timeout()


def test_the_refusal_does_not_claim_earlier_files_are_untouched() -> None:
    clock = _Clock()
    budget = PhaseBudget(clock, 10, "copy", 60)
    clock.now += 10.0
    with pytest.raises(PublishRefusedError) as raised:
        budget.timeout()
    assert "untouched" not in str(raised.value)
    assert "copy budget of this cycle is spent" in str(raised.value)


def test_control_a_whole_number_of_seconds_left_is_granted_up_to_the_cap() -> None:
    clock = _Clock()
    budget = PhaseBudget(clock, 100, "copy", 60)
    assert budget.timeout() == 60
    clock.now += 70.0  # 30 s left
    assert budget.timeout() == 30
