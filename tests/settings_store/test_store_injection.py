# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (Task 19): the floor fill tick and its overlay reader reach the settings store only through a constructor."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import omnimarket
from omnimarket.handlers.overlay_store.reader_store import (
    OverlayAbsentFromStoreError,
    StoreOverlayReader,
)
from omnimarket.handlers.overlay_store.store_import_check import (
    forbidden_store_imports,
)
from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.models.overlay_store import ModelOverlayStoreRecord, document_digest

SRC = Path(omnimarket.__file__).parent

# The modules that read the store or hand it on. None may import an implementation: the wiring chooses it.
READER_AND_HANDLER_MODULES = (
    "handlers/floor_fill_scheduled_tick.py",
    "handlers/overlay_store/reader_store.py",
    "nodes/node_floor_fill_scheduled_effect/handlers/handler_floor_fill_scheduled.py",
)


def _record(overlay_id: str, document: dict[str, Any]) -> dict[str, Any]:
    return ModelOverlayStoreRecord(
        overlay_id=overlay_id,
        digest=document_digest(document),
        document=document,
        source_ref="ref",
        loaded_by="test",
        loaded_at=datetime(2026, 11, 24, tzinfo=UTC),
    ).model_dump(mode="json")


@pytest.mark.parametrize("relative", READER_AND_HANDLER_MODULES)
def test_store_injection_no_handler_or_reader_module_imports_an_implementation(
    relative: str,
) -> None:
    assert forbidden_store_imports((SRC / relative).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "planted",
    [
        "from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore\n",
        "from omnimarket.handlers.overlay_store.store_valkey import ValkeyOverlayStore\n",
        "from omnimarket.handlers.overlay_store import store_postgres\n",
        "import omnimarket.handlers.overlay_store.store_bus_snapshot\n",
        "import redis\n",
        "from redis.asyncio import Redis\n",
        "import asyncpg\n",
    ],
)
def test_store_injection_import_check_fails_on_a_planted_import(planted: str) -> None:
    assert forbidden_store_imports(f"import os\n{planted}")
    ast.parse(planted)


def test_store_injection_import_check_passes_a_protocol_import() -> None:
    clean = "from omnibase_spi.protocols.storage import ProtocolOverlayStore\n"
    assert forbidden_store_imports(clean) == []


async def test_store_injection_reader_reads_only_through_its_constructor() -> None:
    store = InMemoryOverlayStore()
    await store.write_version(_record("some_overlay", {"key": 1}))
    reader = StoreOverlayReader(store)
    assert await reader.read_document("some_overlay") == {"key": 1}


async def test_store_injection_an_overlay_the_store_lacks_is_absent_not_a_file() -> (
    None
):
    reader = StoreOverlayReader(InMemoryOverlayStore())
    with pytest.raises(OverlayAbsentFromStoreError, match="never_loaded"):
        await reader.read_document("never_loaded")


def test_store_injection_scheduled_handler_takes_the_store_as_a_constructor_argument() -> (
    None
):
    from omnimarket.nodes.node_floor_fill_scheduled_effect.handlers.handler_floor_fill_scheduled import (
        HandlerFloorFillScheduled,
    )

    store = InMemoryOverlayStore()
    handler = HandlerFloorFillScheduled(overlay_store=store)
    assert handler.overlay_store is store
