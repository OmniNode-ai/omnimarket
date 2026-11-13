# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one function a reader of the host table calls to read it from the key-value store (OMN-23983)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_host_table_compute.models.model_host_table import (
    ModelHostTableRow,
)
from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.store_host_keys import (
    StoreHostKeysValkey,
)
from omnimarket.nodes.node_host_table_keys_ingest_effect.models.model_host_table_keys import (
    ModelHostTableKeysOverlay,
)
from omnimarket.nodes.node_host_table_keys_read_effect.handlers.handler_host_table_keys_read import (
    HandlerHostTableKeysRead,
)
from omnimarket.nodes.node_host_table_keys_read_effect.models.model_host_table_keys_read import (
    ModelHostTableKeysReadRequest,
)

OVERLAY_ID = "node_host_table_keys_ingest_effect"


def read_host_table_rows(
    environ: Mapping[str, str],
) -> tuple[ModelHostTableRow, ...] | None:
    """The host rows the key-value store holds, or None when the reading is UNKNOWN (never an empty tuple)."""
    overlay = load_structured_overlay(OVERLAY_ID, ModelHostTableKeysOverlay, environ)
    handler = HandlerHostTableKeysRead(
        store=StoreHostKeysValkey.from_environment(overlay, environ),
        overlay=overlay,
        clock=lambda: datetime.now(UTC),
    )
    reading = handler.handle(ModelHostTableKeysReadRequest())
    if reading.document is None:
        return None
    return reading.document.table.rows
