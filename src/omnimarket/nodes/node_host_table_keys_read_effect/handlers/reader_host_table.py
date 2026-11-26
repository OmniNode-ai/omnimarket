# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one function a reader of the host table calls: the table as the injected settings store holds it (Task 20).

The store arrives as the protocol. This module names no implementation, address, key prefix or environment variable,
and reads no file: an absent overlay, a stale heartbeat, an unreachable store and a document that is not a host table
all read ``None``, never an empty tuple.
"""

from __future__ import annotations

from datetime import datetime

from omnibase_spi.protocols.storage import ProtocolOverlayStore
from pydantic import ValidationError

from omnimarket.handlers.overlay_store.tick_from_store import (
    TickStoreUnavailableError,
    read_tick_overlays,
)
from omnimarket.models.host_table import ModelHostTableRequest, ModelHostTableRow
from omnimarket.models.overlay_keys import ModelLabHostsTable
from omnimarket.nodes.node_host_table_compute.handlers.handler_host_table import (
    HandlerHostTable,
    HostTableRefusedError,
)

OVERLAY_ID = "lab_hosts"


async def read_host_table_rows_from_store(
    store: ProtocolOverlayStore,
    *,
    now: datetime,
    max_staleness_seconds: float,
) -> tuple[ModelHostTableRow, ...] | None:
    """The host rows the injected store holds, or None when the reading is UNKNOWN (never an empty tuple)."""
    try:
        documents = await read_tick_overlays(
            store,
            (OVERLAY_ID,),
            now=now,
            max_staleness_seconds=max_staleness_seconds,
        )
        table = ModelLabHostsTable.model_validate(documents[OVERLAY_ID])
        return HandlerHostTable().handle(ModelHostTableRequest(table=table)).rows
    except (TickStoreUnavailableError, ValidationError, HostTableRefusedError):
        return None
