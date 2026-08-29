# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test harness that stands in for the runtime's state_io dispatch seam.

OMN-16924. ``node_session_phase_reducer``'s prior state comes from a
``session_id``-keyed database row that
``omnibase_infra.runtime.auto_wiring.handler_wiring`` loads before ``handle()``
and CAS-persists after it. The handler reaches that row only through
``omnibase_infra``'s ``CONTEXTVAR_STATE_IO_ROWS``.

Tests that want to exercise a multi-event fold therefore have to play the
runtime's part: bind the ContextVar with the encoded prior row, run the fold,
then read back what the contract-declared codec flushes. That is exactly what
:func:`state_io_dispatch` does — and doing it through the REAL codec (not a
hand-built dict) is the point: a codec that stopped round-tripping would fail
these tests rather than pass them on a convenient fixture.

Outside this harness the proxy holds nothing, which is the CLI / pure-unit
shape: no prior state, nothing persisted, no filesystem touched anywhere.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from omnibase_infra.runtime.state_io.state_store_adapter import (
    CONTEXTVAR_STATE_IO_ROWS,
)

from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
    ModelSessionPhaseState,
)
from omnimarket.nodes.node_session_phase_reducer.state_codec import (
    StateIoCodec,
    encode,
    reset_default_proxy,
)


class StateIoRowStore:
    """The durable row store, in memory — one encoded payload per session_id.

    Stands in for the ``session_phase_state`` table so a test can assert what
    the runtime WOULD have persisted without a live Postgres.
    """

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, int]] = {}

    def seed(self, state: ModelSessionPhaseState) -> None:
        """Pre-load a session's row, as if an earlier fold had committed it."""
        self.rows[state.session_id] = (encode(state).decode("utf-8"), 0)

    def load(self, session_id: str) -> ModelSessionPhaseState | None:
        """Decode the committed row for ``session_id``, or ``None``."""
        row = self.rows.get(session_id)
        if row is None:
            return None
        return StateIoCodec().decode(row[0])


@contextmanager
def state_io_dispatch(store: StateIoRowStore, session_id: str) -> Iterator[None]:
    """Run one dispatch for ``session_id`` against ``store``.

    Mirrors ``handler_wiring._load_handle_persist``: bind the loaded row (always
    a *set* value, ``None`` payload when no row exists — that is how the proxy
    distinguishes "state_io active, no row yet" from "state_io inactive"), run
    the body, then persist whatever ``StateIoCodec.flush`` hands back.
    """
    reset_default_proxy()
    flushed: str | None = None
    payload_json: str | None
    payload_json, version = store.rows.get(session_id, (None, 0))
    token = CONTEXTVAR_STATE_IO_ROWS.set({session_id: (payload_json, version)})
    try:
        yield
        flushed = StateIoCodec().flush(session_id)
    finally:
        CONTEXTVAR_STATE_IO_ROWS.reset(token)
    if flushed is not None:
        store.rows[session_id] = (flushed, version + 1)


__all__: list[str] = ["StateIoRowStore", "state_io_dispatch"]
