# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay documents one floor fill tick reads, taken from the injected settings store and nowhere else.

``FAIL_CLOSED`` (the contract's ``config_plane.on_unreachable``): a store that does not answer, an overlay the store does
not hold, and an overlay whose heartbeat is older than ``max_staleness_seconds`` all end the tick the same way, with the
reason named. Nothing here reads a file or a copy on disk.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from omnibase_spi.protocols.storage import ProtocolOverlayStore
from pydantic import JsonValue

from omnimarket.handlers.overlay_store.reader_store import (
    OverlayAbsentFromStoreError,
    StoreOverlayReader,
)

EXIT_STORE_UNAVAILABLE = 75


class TickStoreUnavailableError(RuntimeError):
    """The tick must end degraded: the message names the overlay and why."""


async def read_tick_overlays(
    store: ProtocolOverlayStore,
    overlay_ids: Iterable[str],
    *,
    now: datetime,
    max_staleness_seconds: float,
) -> dict[str, dict[str, JsonValue]]:
    """Each overlay's document, or ``TickStoreUnavailableError`` naming the first overlay that cannot be trusted."""
    reader = StoreOverlayReader(store)
    documents: dict[str, dict[str, JsonValue]] = {}
    for overlay_id in overlay_ids:
        try:
            record = await reader.read_record(overlay_id)
        except OverlayAbsentFromStoreError as exc:
            raise TickStoreUnavailableError(f"absent:{overlay_id}: {exc}") from exc
        except (
            Exception
        ) as exc:  # any backend failure is one fact: the store did not answer
            raise TickStoreUnavailableError(
                f"unreachable:{overlay_id}: {type(exc).__name__}"
            ) from exc
        _require_fresh(overlay_id, record, now, max_staleness_seconds)
        document = record.get("document")
        if not isinstance(document, dict):
            raise TickStoreUnavailableError(f"absent:{overlay_id}: no document")
        documents[overlay_id] = document
    return documents


def _require_fresh(
    overlay_id: str,
    record: Mapping[str, JsonValue],
    now: datetime,
    max_staleness_seconds: float,
) -> None:
    beat = record.get("heartbeat_at")
    if not isinstance(beat, str):
        raise TickStoreUnavailableError(
            f"stale:{overlay_id}: the record carries no heartbeat"
        )
    age = (now - datetime.fromisoformat(beat)).total_seconds()
    if age > max_staleness_seconds:
        raise TickStoreUnavailableError(
            f"stale:{overlay_id}: heartbeat is {age:.0f}s old, bound {max_staleness_seconds:.0f}s"
        )


__all__ = ["EXIT_STORE_UNAVAILABLE", "TickStoreUnavailableError", "read_tick_overlays"]
