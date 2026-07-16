# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the OCC auto-authoring N=10 window counter (OMN-14393).

Proves deliverable 4(c): the counter increments correctly on passing companions,
resets on a non-clean observation, and reaches flip_ready only at N consecutive
clean machine-minted passes — the evidence gate for the future flip.
"""

from __future__ import annotations

import pytest

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.nodes.node_occ_autoauthor_window.handlers.handler_occ_autoauthor_window import (
    HandlerOccAutoauthorWindow,
    aggregate_autoauthor_window,
)
from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_request import (
    ModelOccAutoauthorWindowRequest,
)


def _obs(
    n: int,
    *,
    minted: bool = True,
    match: bool = True,
    eligible: bool = True,
    ts: str | None = None,
) -> ModelOccAutoauthorObservation:
    """One observation; clean by default. ``ts`` defaults to an n-ordered stamp."""
    return ModelOccAutoauthorObservation(
        product_repo="OmniNode-ai/omnimarket",
        product_pr_number=n,
        occ_pr_number=1000 + n,
        minted_by_node=minted,
        attestation_match=match,
        occ_preflight_eligible=eligible,
        observed_at=ts or f"2026-07-16T00:{n:02d}:00Z",
    )


@pytest.mark.unit
def test_empty_trail_is_not_flip_ready() -> None:
    result = aggregate_autoauthor_window(ModelOccAutoauthorWindowRequest())
    assert result.consecutive_clean == 0
    assert result.total_observations == 0
    assert result.flip_ready is False


@pytest.mark.unit
def test_counter_increments_on_consecutive_clean_passes() -> None:
    obs = tuple(_obs(i) for i in range(1, 6))
    result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(observations=obs, required_streak=10)
    )
    assert result.consecutive_clean == 5
    assert result.total_observations == 5
    assert result.flip_ready is False  # 5 < 10
    assert result.streak_broken_by == ""


@pytest.mark.unit
def test_flip_ready_at_exactly_n() -> None:
    obs = tuple(_obs(i) for i in range(1, 11))  # 10 clean
    result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(observations=obs, required_streak=10)
    )
    assert result.consecutive_clean == 10
    assert result.flip_ready is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_field",
    ["minted", "match", "eligible"],
)
def test_any_non_clean_dimension_resets_the_streak(bad_field: str) -> None:
    # 4 clean, then one observation failing exactly one dimension, then 3 clean.
    kwargs = {bad_field: False}
    obs = (
        *[_obs(i) for i in range(1, 5)],
        _obs(5, **kwargs),  # type: ignore[arg-type]
        *[_obs(i) for i in range(6, 9)],
    )
    result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(observations=obs, required_streak=10)
    )
    # Trailing streak is only the 3 clean AFTER the reset.
    assert result.consecutive_clean == 3
    assert result.total_observations == 8
    assert result.flip_ready is False
    assert result.streak_broken_by == "OmniNode-ai/omnimarket#5"


@pytest.mark.unit
def test_streak_is_measured_from_the_end_not_the_max_run() -> None:
    # A long clean run, a break, then a short clean run: the trailing (short) run
    # is what counts — 10 earlier clean passes do NOT make it flip_ready.
    obs = (
        *[_obs(i) for i in range(1, 11)],  # 10 clean
        _obs(11, match=False),  # reset
        _obs(12),  # 1 clean trailing
    )
    result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(observations=obs, required_streak=10)
    )
    assert result.consecutive_clean == 1
    assert result.flip_ready is False


@pytest.mark.unit
def test_counter_is_order_independent() -> None:
    # Same observations, shuffled input order → same trailing streak (sorted by ts).
    ordered = [_obs(i) for i in range(1, 6)]
    shuffled = [ordered[3], ordered[0], ordered[4], ordered[1], ordered[2]]
    r1 = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(observations=tuple(ordered))
    )
    r2 = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(observations=tuple(shuffled))
    )
    assert r1.consecutive_clean == r2.consecutive_clean == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_wraps_the_pure_counter() -> None:
    obs = tuple(_obs(i) for i in range(1, 11))
    result = await HandlerOccAutoauthorWindow().handle(
        ModelOccAutoauthorWindowRequest(observations=obs, required_streak=10)
    )
    assert result.flip_ready is True
    assert result.consecutive_clean == 10


@pytest.mark.unit
def test_required_streak_must_be_positive() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        ModelOccAutoauthorWindowRequest(required_streak=0)
