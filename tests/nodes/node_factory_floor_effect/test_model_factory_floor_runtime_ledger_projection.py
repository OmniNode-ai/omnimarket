# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor overlay accepts a ``ledger_projection`` block (OMN-23963, OMN-24490).

The overlay model forbids unknown keys, so an overlay that declares the work
ledger projection window was refused by name until the model gained the field.
These tests pin the field: absent by default, accepted when declared, strict
inside the block.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorOverlay,
    ModelLedgerProjectionWindow,
)

_BLOCK: dict[str, Any] = {
    "reader": "work_ledger_window",
    "cache_ttl_seconds": 600,
    "read_workers": 4,
    "settle_seconds": 300,
}


def _overlay(**extra: Any) -> ModelFactoryFloorOverlay:
    return ModelFactoryFloorOverlay.model_validate(
        {
            "plan_path_contains": ["plans/"],
            "processes": [],
            "roles": [],
            "release_required_repos": [],
            "held_gates": [],
            **extra,
        }
    )


@pytest.mark.unit
def test_factory_floor_runtime_ledger_projection_is_absent_by_default() -> None:
    assert _overlay().ledger_projection is None
    assert ModelFactoryFloorOverlay.model_fields["ledger_projection"].default is None


@pytest.mark.unit
def test_factory_floor_runtime_overlay_accepts_a_ledger_projection_block() -> None:
    overlay = _overlay(ledger_projection=_BLOCK)
    assert overlay.ledger_projection == ModelLedgerProjectionWindow(**_BLOCK)
    assert overlay.ledger_projection is not None
    assert overlay.ledger_projection.reader == "work_ledger_window"


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        {**_BLOCK, "unknown_key": 1},
        {**_BLOCK, "reader": ""},
        {**_BLOCK, "cache_ttl_seconds": 0},
        {**_BLOCK, "read_workers": 0},
        {**_BLOCK, "settle_seconds": -1},
    ],
)
def test_factory_floor_runtime_ledger_projection_block_is_strict(
    bad: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        _overlay(ledger_projection=bad)


@pytest.mark.unit
def test_factory_floor_runtime_overlay_still_refuses_an_unknown_top_level_key() -> None:
    # Positive control: the model is still strict, so the accept test above is
    # not passing because extra keys are allowed.
    with pytest.raises(ValidationError):
        _overlay(ledger_projection_typo=_BLOCK)
