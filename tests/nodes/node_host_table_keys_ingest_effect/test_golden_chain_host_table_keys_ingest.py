# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the host table keys ingest node (OMN-23983): contract, signature, one record in, one key out."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.models.host_table_keys import (
    ModelHostTableKeysDocument,
)
from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.handler_host_table_keys_ingest import (
    HandlerHostTableKeysIngest,
)
from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.store_host_keys import (
    StoreHostKeysInMemory,
)
from tests.nodes.node_host_table_compute.table_support import (
    EXCLUDED,
    REASON,
    three_hosts,
)
from tests.nodes.node_host_table_keys_ingest_effect.support import (
    NOW,
    PREFIX,
    overlay,
    record,
    registry_document,
)

pytestmark = pytest.mark.unit


def test_contract_golden_chain() -> None:
    package = importlib.import_module(
        "omnimarket.nodes.node_host_table_keys_ingest_effect"
    )
    assert package.__file__ is not None
    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    assert contract["name"] == "node_host_table_keys_ingest_effect"
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.evt.omnimarket.overlay-store-loaded.v1"
    ]
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

    # Nothing consumes a result of this node and it declares no publish topic, so a declared output model
    # would leave every dispatch with a result and no applier (the undeliverable-output path).
    assert "output_model" not in contract
    assert "output_model" not in binding
    assert "terminal_events" not in contract.get("runtime_dispatch", {})
    assert contract["event_bus"]["publish_topics"] == []
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is model(binding["input_model"])
    assert hints["return"] is type(None)


def test_one_record_writes_one_key_holding_the_node_rows() -> None:
    store = StoreHostKeysInMemory()
    handler = HandlerHostTableKeysIngest(
        store=store, overlay=overlay(), clock=lambda: NOW
    )
    incoming = record(registry_document(*three_hosts()))
    assert handler.handle(incoming) is None
    stored = store.get(f"{PREFIX}:table")
    assert stored is not None
    document = ModelHostTableKeysDocument.model_validate_json(stored)
    assert document.digest == incoming.digest
    assert len(document.table.rows) == 3
    assert len(document.table.admitted) == 2
    assert [r.name for r in document.table.rows] == [
        "admitted-a",
        EXCLUDED,
        "admitted-b",
    ]
    assert [(r.name, r.exclusion_reason) for r in document.table.excluded] == [
        (EXCLUDED, REASON)
    ]
