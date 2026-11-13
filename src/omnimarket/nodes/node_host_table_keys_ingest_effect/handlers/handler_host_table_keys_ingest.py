# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ingest the lab host table into the key-value store when the overlay loads (OMN-23983).

Key layout: one key, f"{overlay.key_prefix}:table", holding the whole document (digest, source reference, load
time, ingest time and the host table node's row set), so a reader never sees half a table. A refused record
writes nothing, so the key already stored stays.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pydantic import ValidationError

from omnimarket.models.overlay_keys import ModelLabHostsTable
from omnimarket.models.overlay_store.model_overlay_store import ModelOverlayStoreRecord
from omnimarket.nodes.node_host_table_compute.handlers.handler_host_table import (
    HandlerHostTable,
    HostTableRefusedError,
)
from omnimarket.nodes.node_host_table_compute.models.model_host_table import (
    ModelHostTableRequest,
)

from ..models.model_host_table_keys import (
    ModelHostTableKeysDocument,
    ModelHostTableKeysIngestResult,
    ModelHostTableKeysOverlay,
)
from .store_host_keys import ProtocolHostKeyStore


class HostTableKeysRefusedError(ValueError):
    """The record could not be ingested: the overlay or the table refused it."""


class HandlerHostTableKeysIngest:
    """EFFECT handler: the overlay store record to the key-value store."""

    def __init__(
        self,
        *,
        store: ProtocolHostKeyStore,
        overlay: ModelHostTableKeysOverlay,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._overlay = overlay
        self._clock = clock

    def handle(
        self, request: ModelOverlayStoreRecord
    ) -> ModelHostTableKeysIngestResult:
        key = f"{self._overlay.key_prefix}:table"
        if request.overlay_id != self._overlay.overlay_id:
            return ModelHostTableKeysIngestResult(
                outcome="ignored", key=key, digest="", hosts=0, admitted=0
            )
        try:
            table = ModelLabHostsTable.model_validate(request.document)
            result = HandlerHostTable().handle(ModelHostTableRequest(table=table))
        except (HostTableRefusedError, ValidationError) as exc:
            raise HostTableKeysRefusedError(
                f"overlay {request.overlay_id}: {exc}"
            ) from exc
        document = ModelHostTableKeysDocument(
            digest=request.digest,
            source_ref=request.source_ref,
            loaded_at=request.loaded_at,
            ingested_at=self._clock(),
            table=result,
        )
        self._store.set(key, document.model_dump_json())
        return ModelHostTableKeysIngestResult(
            outcome="written",
            key=key,
            digest=request.digest,
            hosts=len(result.rows),
            admitted=len(result.admitted),
        )
