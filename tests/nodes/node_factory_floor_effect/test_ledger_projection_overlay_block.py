# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor overlay accepts the ledger_projection block the overlays repo carries (OMN-24490)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelLedgerProjectionWindow,
)

_BLOCK = {
    "reader": "factory-floor",
    "cache_ttl_seconds": 21600,
    "read_workers": 4,
    "settle_seconds": 3600,
}


def test_block_loads() -> None:
    window = ModelLedgerProjectionWindow.model_validate(_BLOCK)
    assert window.reader == "factory-floor"
    assert window.settle_seconds == 3600


def test_unknown_key_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLedgerProjectionWindow.model_validate({**_BLOCK, "extra": 1})


def test_empty_reader_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLedgerProjectionWindow.model_validate({**_BLOCK, "reader": ""})
