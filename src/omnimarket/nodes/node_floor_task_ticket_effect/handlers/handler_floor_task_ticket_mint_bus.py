# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus handler of node_floor_task_ticket_effect: a mint command on the command topic is answered on the completed topic.

The overlay is read on every command, not at construction, so a runtime that loads the package without the overlay
still boots. An overlay the loader cannot read, and a store the factory cannot build, end the command with a typed
unavailable answer on the completed topic: the overlay's error text names the missing or invalid keys, a store
failure carries the exception type name only (its text may hold deployment facts and is never put on a record).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from omnibase_spi.protocols.lane import ProtocolFloorTicketStore

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers.handler_floor_task_ticket_mint import (
    HandlerFloorTaskTicketMint,
)
from omnimarket.nodes.node_floor_task_ticket_effect.models import (
    ModelFloorTaskTicketBusOverlay,
    ModelFloorTaskTicketMintCommand,
    ModelFloorTaskTicketMintCompleted,
    ModelFloorTaskTicketMintRequest,
    ModelFloorTaskTicketMintResult,
    ModelFloorTaskTicketOverlayDocument,
)

_LOG = logging.getLogger(__name__)
_CAUSE_TEXT_LIMIT = 300


class HandlerFloorTaskTicketMintBus:
    """Answer a mint command over the bus with the same typed result the in-process mint returns."""

    def __init__(
        self,
        *,
        overlay_loader: Callable[[], ModelFloorTaskTicketBusOverlay] | None = None,
        store_factory: Callable[[str, str, float], ProtocolFloorTicketStore]
        | None = None,
    ) -> None:
        self._overlay_loader = (
            overlay_loader if overlay_loader is not None else self._read_overlay
        )
        self._store_factory = (
            store_factory if store_factory is not None else self._build_store
        )
        self._stores: dict[tuple[str, str], ProtocolFloorTicketStore] = {}

    @staticmethod
    def _read_overlay() -> ModelFloorTaskTicketBusOverlay:
        return load_structured_overlay(
            "node_floor_task_ticket_effect", ModelFloorTaskTicketOverlayDocument
        ).floor_fill

    @staticmethod
    def _build_store(
        kind: str, lane: str, timeout_seconds: float
    ) -> ProtocolFloorTicketStore:
        if kind == "inmemory":
            from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
                InMemoryFloorTicketStore,
            )

            return InMemoryFloorTicketStore()
        if kind == "linear":
            from omnimarket.handlers.floor_task_ticket_linear_store import (
                build_linear_store,
            )

            return build_linear_store(lane, timeout_seconds=timeout_seconds)
        raise NodeOverlayError(
            f"overlay floor_fill.ticket_store is not inmemory or linear: {kind!r}"
        )

    async def handle(
        self, command: ModelFloorTaskTicketMintCommand
    ) -> ModelFloorTaskTicketMintCompleted:
        try:
            overlay = self._overlay_loader()
        except NodeOverlayError as exc:
            return self._answered(
                command,
                ModelFloorTaskTicketMintResult(
                    state="unavailable",
                    cause="overlay-unavailable:" + str(exc)[:_CAUSE_TEXT_LIMIT],
                ),
            )
        except Exception as exc:
            return self._answered(
                command,
                ModelFloorTaskTicketMintResult(
                    state="unavailable",
                    cause=f"overlay-unavailable:{type(exc).__name__}",
                ),
            )

        try:
            # The in-memory store keeps its tickets between commands, so it is one per process; the tracker store carries the
            # filing lane's identity into the create gate, so it is one per lane.
            key = (
                overlay.ticket_store,
                command.requested_by_lane if overlay.ticket_store == "linear" else "",
            )
            store = self._stores.get(key)
            if store is None:
                store = self._store_factory(
                    overlay.ticket_store,
                    command.requested_by_lane,
                    overlay.mint_timeout_seconds,
                )
                self._stores[key] = store
        except Exception as exc:
            # The exception text may carry deployment facts; only the type name
            # goes on the log line and the completed record.
            _LOG.warning(
                "floor task ticket bus: store build failed (%s)", type(exc).__name__
            )
            return self._answered(
                command,
                ModelFloorTaskTicketMintResult(
                    state="unavailable",
                    cause=f"store-unavailable:{type(exc).__name__}",
                ),
            )

        request = ModelFloorTaskTicketMintRequest(
            floor_key=command.floor_key,
            title=command.title,
            description=command.description,
            parent=command.parent,
            mint_timeout_seconds=overlay.mint_timeout_seconds,
        )
        result = await asyncio.to_thread(
            HandlerFloorTaskTicketMint(store).handle, request
        )
        return self._answered(command, result)

    @staticmethod
    def _answered(
        command: ModelFloorTaskTicketMintCommand,
        result: ModelFloorTaskTicketMintResult,
    ) -> ModelFloorTaskTicketMintCompleted:
        return ModelFloorTaskTicketMintCompleted(
            correlation_id=command.correlation_id,
            requested_by_lane=command.requested_by_lane,
            floor_key=command.floor_key,
            result=result,
        )
