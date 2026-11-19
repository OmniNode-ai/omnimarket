"""ModelFactoryFloorOverlay accepts the ledger_projection block the overlays-main document carries."""

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorLedgerProjection,
)


def test_ledger_projection_block_is_accepted() -> None:
    block = ModelFactoryFloorLedgerProjection.model_validate(
        {
            "reader": "factory-floor",
            "cache_ttl_seconds": 21600,
            "read_workers": 4,
            "settle_seconds": 3600,
        }
    )
    assert block.reader == "factory-floor"
    assert block.read_workers == 4


def test_control_unknown_projection_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelFactoryFloorLedgerProjection.model_validate(
            {"reader": "factory-floor", "cache_ttl_seconds": 1, "bogus": 1}
        )
