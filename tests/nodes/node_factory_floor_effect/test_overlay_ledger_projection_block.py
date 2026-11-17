# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor overlay accepts the ``ledger_projection`` block the overlays repository's main branch carries (OMN-25348).

The overlays repository's main branch holds ``ledger_projection`` in the floor document. The overlay-main-load check judges that
document through this tree's own ``ModelFactoryFloorOverlay``, whose ``extra="forbid"`` refuses a key the model lacks, so a tree whose
model has no such key reads the check red on every pull request.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorOverlay,
)

pytestmark = pytest.mark.unit

# The block as the overlays repository's main branch carries it for node_factory_floor_effect.
PROJECTION_BLOCK: dict[str, Any] = {
    "reader": "factory-floor",
    "cache_ttl_seconds": 21600,
    "read_workers": 4,
    "settle_seconds": 3600,
}


def _document(**extra: Any) -> dict[str, Any]:
    return {
        "plan_path_contains": ["x"],
        "processes": [],
        "roles": [],
        "release_required_repos": [],
        "held_gates": [],
        **extra,
    }


def test_the_overlay_main_block_loads_through_the_floor_model() -> None:
    overlay = ModelFactoryFloorOverlay.model_validate(
        _document(ledger_projection=PROJECTION_BLOCK)
    )
    assert overlay.ledger_projection is not None
    assert overlay.ledger_projection.model_dump() == PROJECTION_BLOCK


def test_an_overlay_without_the_block_still_loads() -> None:
    overlay = ModelFactoryFloorOverlay.model_validate(_document())
    assert overlay.ledger_projection is None


@pytest.mark.parametrize(
    "broken",
    [
        {**PROJECTION_BLOCK, "unknown_key": 1},
        {**PROJECTION_BLOCK, "cache_ttl_seconds": 0},
        {**PROJECTION_BLOCK, "reader": "Not A Reader"},
        {k: v for k, v in PROJECTION_BLOCK.items() if k != "settle_seconds"},
    ],
    ids=["unknown-key", "zero-ttl", "bad-reader-name", "missing-settle-seconds"],
)
def test_error_chain_a_malformed_block_is_refused(broken: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelFactoryFloorOverlay.model_validate(_document(ledger_projection=broken))
