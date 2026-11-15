# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stand-in for the runtime's state_io dispatch seam, over the panel reducer's own codec (OMN-19998).

Follows tests/session_phase_state_io_harness.py: bind the loaded row the way the runtime does, run the handler, persist what the
node's codec flushes with the version bumped, and skip the save when the payload is byte-identical (as the production wrapper does).
The production wrapper itself is driven in test_production_wrapper_cas.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from omnibase_infra.runtime.state_io.state_store_adapter import (
    CONTEXTVAR_STATE_IO_ROWS,
)

from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    ModelWorkStateProjectedEvent,
)
from omnimarket.nodes.node_work_state_panel_reducer.state_codec import (
    StateIoCodec,
    reset_default_proxy,
)


class StateIoRowStore:
    """The durable rows in memory: key -> (payload json, version)."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, int]] = {}
        self.saves: list[int] = []


@contextmanager
def state_io_dispatch(store: StateIoRowStore, key: str) -> Iterator[None]:
    reset_default_proxy()
    payload_json, version = store.rows.get(key, (None, 0))
    token = CONTEXTVAR_STATE_IO_ROWS.set({key: (payload_json, version)})
    flushed: str | None = None
    try:
        yield
        flushed = StateIoCodec().flush(key)
    finally:
        CONTEXTVAR_STATE_IO_ROWS.reset(token)
    if flushed is not None and flushed != payload_json:
        new_version = version + 1 if key in store.rows else 0
        store.rows[key] = (flushed, new_version)
        store.saves.append(new_version)


def run_event(
    store: StateIoRowStore, handler: Any, event: ModelWorkStateProjectedEvent
) -> dict[str, Any]:
    with state_io_dispatch(store, event.projector.value):
        result: dict[str, Any] = handler.handle(event)
    return result
