# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A liveness kind with surrounding whitespace is refused, not kept to match nothing (OMN-22888 review finding)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_floor_fill_owner_count_compute.models.model_floor_fill_owner_count import (
    ModelFloorFillOwnerCountOverlay,
)

pytestmark = pytest.mark.unit


def _overlay(kinds: tuple[str, ...]) -> ModelFloorFillOwnerCountOverlay:
    return ModelFloorFillOwnerCountOverlay(
        lane_write_window_minutes=60, liveness_kinds=kinds, emission_piece=6
    )


@pytest.mark.parametrize("kinds", [(" CLAIM ", " TERMINAL "), ("CLAIM ",), (" CLAIM",)])
def test_a_kind_with_surrounding_whitespace_is_refused(kinds: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError, match="non-empty upper-case row kind"):
        _overlay(kinds)


def test_plain_upper_case_kinds_are_still_accepted() -> None:
    assert _overlay(("CLAIM", "TERMINAL")).liveness_kinds == ("CLAIM", "TERMINAL")
