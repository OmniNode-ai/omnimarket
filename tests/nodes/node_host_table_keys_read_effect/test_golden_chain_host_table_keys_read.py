# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the host table keys read node (OMN-23983): contract, signature, one key in, one reading out."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.store_host_keys import (
    StoreHostKeysInMemory,
)
from omnimarket.nodes.node_host_table_keys_read_effect.handlers.handler_host_table_keys_read import (
    HandlerHostTableKeysRead,
)
from tests.nodes.node_host_table_keys_ingest_effect.support import NOW, overlay

pytestmark = pytest.mark.unit


def test_contract_golden_chain() -> None:
    package = importlib.import_module(
        "omnimarket.nodes.node_host_table_keys_read_effect"
    )
    assert package.__file__ is not None
    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    assert contract["name"] == "node_host_table_keys_read_effect"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["overlay"]["node"] == "node_host_table_keys_ingest_effect"
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert issubclass(result, BaseModel)
        return result

    request_class = model(binding["input_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is model(binding["output_model"])
    reading = handler_class(
        store=StoreHostKeysInMemory(), overlay=overlay(), clock=lambda: NOW
    ).handle(request_class())
    assert isinstance(reading, hints["return"])
    assert reading.state == "UNKNOWN"
    assert HandlerHostTableKeysRead is handler_class
