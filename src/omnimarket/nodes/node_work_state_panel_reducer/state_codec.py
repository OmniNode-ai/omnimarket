# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable-state codec and request-scoped proxy for ``ModelWorkStatePanelRow``.

The panel reducer's state of record is one row per ``projector`` (the event's
``projector`` string, the contract-declared ``state_io.key``), supplied to and
read back from the handler by the runtime's ``state_io`` dispatch seam. The
handler performs NO I/O of any kind: this module is the only encode/decode
surface between the handler and the durable row, and there is deliberately no
fallback store — when the seam's ContextVar is unbound (the CLI, a unit test,
any caller outside the dispatch) the proxy simply has no prior state and nothing
is persisted.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, MutableMapping

from omnimarket.models.work_state_panel.model_work_state_panel_row import (
    ModelWorkStatePanelRow,
)

# omnibase_infra's state_io wiring seam reads three well-known top-level JSON
# keys off an otherwise-opaque payload to populate the durable row's
# denormalized columns (``_extract_state_io_metadata``). ``ModelWorkStatePanelRow``
# has no field of any of these names, so ``encode`` injects them and ``decode``
# strips them before validating.
#
# * ``state`` — the row's indexed FSM-state column: the row's own ``state``
#   property (``HELD`` while it holds a publication, ``EMPTY`` while it only
#   records its last refusal). It is never one of infra's terminal names
#   (``COMPLETED``/``FAILED``); this contract declares no ``published_events``,
#   so it has no outbox and nothing to recover.
# * ``in_flight`` — always False. A reducer fold is complete when it returns;
#   there is no committed-but-unpublished emission to mark.
# * ``tenant_id`` — the work-state panel row carries no tenant, so this is the
#   empty string. The column is denormalized provenance, never an authorization
#   key.
_STATE_KEY = "state"
_IN_FLIGHT_KEY = "in_flight"
_TENANT_ID_KEY = "tenant_id"
_INJECTED_KEYS = (_STATE_KEY, _IN_FLIGHT_KEY, _TENANT_ID_KEY)


def encode(row: ModelWorkStatePanelRow) -> bytes:
    """Serialize a panel row to JSON bytes for durable storage."""
    payload = row.model_dump(mode="json")
    payload[_STATE_KEY] = row.state.value
    payload[_IN_FLIGHT_KEY] = False
    payload[_TENANT_ID_KEY] = ""
    return json.dumps(payload).encode("utf-8")


def decode(raw: bytes | str) -> ModelWorkStatePanelRow:
    """Deserialize a durably-stored row payload back into a panel row.

    Strips the well-known keys ``encode`` injects — ``ModelWorkStatePanelRow``
    declares ``extra="forbid"`` and has no field of those names.
    """
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(
            f"work_state_panel_state row payload is not a JSON object "
            f"(got {type(payload).__name__})"
        )
    for key in _INJECTED_KEYS:
        payload.pop(key, None)
    return ModelWorkStatePanelRow(**payload)


def _read_active_rows() -> dict[str, tuple[str | None, int]] | None:
    """Return omnibase_infra's state_io ContextVar value, or ``None``.

    Lazy, ImportError-tolerant import — omnimarket's pinned ``omnibase-infra``
    release may predate this symbol. ``ImportError`` degrades to "state_io
    inactive", exactly like an unset ContextVar, so this module never crashes
    ahead of the paired infra release; once the new infra is deployed the import
    succeeds and the bridge activates with no code change here.
    """
    try:
        from omnibase_infra.runtime.state_io.state_store_adapter import (
            CONTEXTVAR_STATE_IO_ROWS,
        )
    except ImportError:
        return None
    return CONTEXTVAR_STATE_IO_ROWS.get()


class WorkStatePanelStateProxy(MutableMapping[str, ModelWorkStatePanelRow]):
    """``MutableMapping`` view over durable per-projector panel rows.

    Keyed on the event's ``projector`` — the contract's declared ``state_io.key``
    and the reducer's actual identity: one row per projector, so the ``unread``
    and ``file_sources`` profiles fold into separate rows.

    Bound (a state_io dispatch is in flight): reads decode the bound row's raw
    JSON once and cache the decoded object for the rest of the dispatch, so a
    handler that reads twice sees one object. :meth:`flush` re-encodes and
    evicts it for the runtime's post-handle CAS-persist.

    Unbound: there is no prior state and nothing to persist. Reads miss, writes
    land in the per-dispatch cache and are dropped when it is next flushed.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str | None, ModelWorkStatePanelRow]] = {}

    def _lookup(self, key: str) -> ModelWorkStatePanelRow | None:
        """Resolve ``key``'s row for the current dispatch, or ``None``."""
        rows = _read_active_rows() or {}
        payload_json, _version = rows.get(key, (None, 0))
        cached = self._cache.get(key)
        if cached is not None and cached[0] is payload_json:
            return cached[1]
        if payload_json is None:
            return None
        decoded = decode(payload_json)
        self._cache[key] = (payload_json, decoded)
        return decoded

    def __getitem__(self, key: str) -> ModelWorkStatePanelRow:
        row = self._lookup(key)
        if row is None:
            raise KeyError(key)
        return row

    def __setitem__(self, key: str, row: ModelWorkStatePanelRow) -> None:
        rows = _read_active_rows() or {}
        payload_json, _version = rows.get(key, (None, 0))
        self._cache[key] = (payload_json, row)

    def __delitem__(self, key: str) -> None:
        del self._cache[key]

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return self._lookup(key) is not None

    def __iter__(self) -> Iterator[str]:
        return iter(self._cache)

    def __len__(self) -> int:
        return len(self._cache)

    def flush(self, key: str) -> bytes:
        """Re-encode and evict ``key``'s row for the CAS-persist step.

        Raises ``KeyError`` when the key was never read or written on this proxy
        during the current dispatch — nothing to persist.
        """
        _source, row = self._cache.pop(key)
        return encode(row)


_default_proxy: WorkStatePanelStateProxy | None = None


def get_default_proxy() -> WorkStatePanelStateProxy:
    """Return the process-wide default :class:`WorkStatePanelStateProxy`.

    Exactly one ``HandlerWorkStatePanelReducer`` is constructed in production
    (the auto-wiring handler resolver instantiates it once at wiring time), so a
    singleton changes no production behavior. It exists so
    :meth:`StateIoCodec.flush` — resolved independently by the runtime from this
    node's contract-declared ``state_io.codec`` reference — reaches the SAME
    per-dispatch cache the handler populated during the just-completed
    ``handle()`` call, without a direct object reference between the two.
    """
    global _default_proxy
    if _default_proxy is None:
        _default_proxy = WorkStatePanelStateProxy()
    return _default_proxy


def reset_default_proxy() -> None:
    """Drop the process-wide proxy. Test isolation only."""
    global _default_proxy
    _default_proxy = None


class StateIoCodec:
    """Contract-declared ``state_io.codec`` bridging the runtime dispatch seam.

    Resolved independently by ``omnibase_infra.runtime.auto_wiring.handler_wiring``
    from this node's ``contract.yaml`` ``state_io.codec`` ``{module, name}``
    reference and instantiated once at wiring time. ``flush`` is the explicit
    post-handle bridge the runtime calls to read back whatever the handler folded
    into :class:`WorkStatePanelStateProxy` during the just-completed ``handle()``.
    """

    def encode(self, row: ModelWorkStatePanelRow) -> bytes:
        return encode(row)

    def decode(self, raw: bytes | str) -> ModelWorkStatePanelRow:
        return decode(raw)

    def flush(self, key: str) -> str | None:
        """Re-encode the proxy's entry for ``key``, if touched this dispatch.

        ``key`` is the contract-declared ``state_io.key`` value for the message
        just dispatched — the event's ``projector``. Returns ``None`` when the
        key was never read or written on the shared default proxy during this
        dispatch; the runtime then treats the row as unchanged and skips
        persistence.
        """
        try:
            encoded = get_default_proxy().flush(key)
        except KeyError:
            return None
        return encoded.decode("utf-8")


__all__: list[str] = [
    "StateIoCodec",
    "WorkStatePanelStateProxy",
    "decode",
    "encode",
    "get_default_proxy",
    "reset_default_proxy",
]
