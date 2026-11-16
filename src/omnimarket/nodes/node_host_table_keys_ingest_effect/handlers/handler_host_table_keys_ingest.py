# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ingest the lab host table into the key-value store when the overlay loads (OMN-23983).

Key layout: one key, f"{overlay.key_prefix}:table", holding the whole document (digest, source reference, load
time, ingest time and the host table node's row set), so a reader never sees half a table. A refused record
writes nothing, so the key already stored stays.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.host_table import (
    ModelHostTableRequest,
)
from omnimarket.models.host_table_keys import (
    ModelHostTableKeysDocument,
    ModelHostTableKeysIngestResult,
    ModelHostTableKeysOverlay,
)
from omnimarket.models.overlay_keys import ModelLabHostsTable
from omnimarket.models.overlay_store.model_overlay_store import ModelOverlayStoreRecord
from omnimarket.nodes.node_host_table_compute.handlers.handler_host_table import (
    HandlerHostTable,
    HostTableRefusedError,
)

from .store_host_keys import ProtocolHostKeyStore, StoreHostKeysValkey

OVERLAY_ID = "node_host_table_keys_ingest_effect"


class HostTableKeysRefusedError(ValueError):
    """The record could not be ingested: the overlay or the table refused it."""


class HandlerHostTableKeysIngest:
    """EFFECT handler: the overlay store record to the key-value store."""

    def __init__(
        self,
        *,
        store: ProtocolHostKeyStore | None = None,
        overlay: ModelHostTableKeysOverlay | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # Given nothing (the runtime's auto-wiring builds the handler bare), the overlay and the store are
        # read from the environment on the first record, so a node with no overlay wires and refuses then.
        self._store = store
        self._overlay = overlay
        self._clock = clock or (lambda: datetime.now(UTC))

    def _resolve(self) -> tuple[ProtocolHostKeyStore, ModelHostTableKeysOverlay]:
        if self._overlay is None:
            self._overlay = load_structured_overlay(
                OVERLAY_ID, ModelHostTableKeysOverlay, os.environ
            )
        if self._store is None:
            self._store = StoreHostKeysValkey.from_environment(
                self._overlay, os.environ
            )
        return self._store, self._overlay

    def handle(
        self, request: ModelOverlayStoreRecord
    ) -> ModelHostTableKeysIngestResult:
        store, overlay = self._resolve()
        key = f"{overlay.key_prefix}:table"
        if request.overlay_id != overlay.overlay_id:
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
        store.set(key, document.model_dump_json())
        return ModelHostTableKeysIngestResult(
            outcome="written",
            key=key,
            digest=request.digest,
            hosts=len(result.rows),
            admitted=len(result.admitted),
        )
