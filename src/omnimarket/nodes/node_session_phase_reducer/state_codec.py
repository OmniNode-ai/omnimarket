# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable-state codec and request-scoped proxy for ``ModelSessionPhaseState``.

The reducer's state of record is a row in the platform database, supplied to
and read back from the handler by the runtime's ``state_io`` dispatch seam.
Previously it was a cwd-relative ``.onex_state/session/phase_state.yaml`` file,
which no runtime container could ever write: the container's cwd is ``/app``
(``root:root 0755``) while the process runs as ``omniinfra``, so every bus
dispatch raised ``PermissionError: [Errno 13] Permission denied: '.onex_state'``
and DLQ'd — a 100% failure rate on all three subscribed topics, on every lane.

Operator ruling, verbatim: *"onex_state should be configurable via contract
overlay right? for our purposes, state should only be kept in the database. if
you disagree, let's have a conversation."*

So: the state lives in the database, the binding is declared in ``contract.yaml``
under ``state_io`` (database/table are ``${env.VAR:default}`` overlay refs), and
the handler performs NO I/O of any kind. This module is the only encode/decode
surface between the two.

How the seam works (``omnibase_infra.runtime.auto_wiring.handler_wiring``):

1. Before ``handle()``, the runtime loads the row keyed on the contract-declared
   ``state_io.key`` (here ``session_id``) and binds
   ``{session_id: (payload_json, version)}`` into ``CONTEXTVAR_STATE_IO_ROWS``.
2. ``handle()`` runs. It reads its prior state through
   :class:`SessionPhaseStateProxy` (a ContextVar read — not I/O), folds, and
   writes the new state back into the same request-scoped proxy.
3. After ``handle()`` returns, the runtime calls
   :meth:`StateIoCodec.flush` and CAS-persists whatever comes back.

There is deliberately NO fallback store. When the ContextVar is unbound (the
CLI, a unit test, any caller outside the dispatch seam) the proxy simply has no
prior state and nothing is persisted — the fold is a pure preview. A
process-local dict or a local file here would be exactly the "state of record
lives somewhere other than the database" the ruling removes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, MutableMapping
from typing import cast

from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
    ModelSessionPhaseState,
)

# omnibase_infra's state_io wiring seam reads three well-known top-level JSON
# keys off an otherwise-opaque payload to populate the durable row's
# denormalized columns (``_extract_state_io_metadata``). ``ModelSessionPhaseState``
# has no field of any of these names, so ``encode`` injects them and ``decode``
# strips them before validating.
#
# * ``state`` — the row's indexed FSM-state column. The session phase FSM's own
#   state IS ``current_phase`` (contract.yaml ``state_machine``), so that is what
#   is projected here. It is never one of infra's terminal names
#   (``COMPLETED``/``FAILED``), which is correct: those gate the in-row OUTBOX
#   recovery sweep, and this contract declares no ``published_events``, so it has
#   no outbox and nothing to recover.
# * ``in_flight`` — always False. A reducer fold is complete when it returns;
#   there is no committed-but-unpublished emission to mark.
# * ``tenant_id`` — the omniclaude session wire contracts carry no tenant, so
#   this is the empty string. The column is denormalized provenance, never an
#   authorization key (the same classification migration 090's
#   ``delegation_workflow_state`` carries).
_STATE_KEY = "state"
_IN_FLIGHT_KEY = "in_flight"
_TENANT_ID_KEY = "tenant_id"
_INJECTED_KEYS = (_STATE_KEY, _IN_FLIGHT_KEY, _TENANT_ID_KEY)


def encode(state: ModelSessionPhaseState) -> bytes:
    """Serialize phase state to JSON bytes for durable storage."""
    payload = state.model_dump(mode="json")
    payload[_STATE_KEY] = state.current_phase
    payload[_IN_FLIGHT_KEY] = False
    payload[_TENANT_ID_KEY] = ""
    return json.dumps(payload).encode("utf-8")


def decode(raw: bytes | str) -> ModelSessionPhaseState:
    """Deserialize a durably-stored row payload back into phase state.

    Strips the well-known keys ``encode`` injects — ``ModelSessionPhaseState``
    declares ``extra="forbid"`` and has no field of those names.
    """
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(
            f"session_phase_state row payload is not a JSON object "
            f"(got {type(payload).__name__})"
        )
    for key in _INJECTED_KEYS:
        payload.pop(key, None)
    return ModelSessionPhaseState(**payload)


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
    return cast(
        "dict[str, tuple[str | None, int]] | None", CONTEXTVAR_STATE_IO_ROWS.get()
    )


# Sentinel for "this cache belongs to no dispatch yet". A plain ``None`` cannot
# serve: ``None`` is the real, meaningful unbound value of ``_read_active_rows``,
# so a fresh proxy would compare equal to the unbound shape and skip its clear.
_UNSCOPED: object = object()


class SessionPhaseStateProxy(MutableMapping[str, ModelSessionPhaseState]):
    """``MutableMapping`` view over durable per-session phase state.

    Keyed on ``session_id`` — the contract's declared ``state_io.key``, and the
    reducer's actual identity: ``HandlerSessionPhaseReducer.delta`` rejects an
    event whose ``session_id`` disagrees with the folded state's, and the
    omniclaude hooks mint a fresh ``correlation_id`` per event, so keying on
    ``correlation_id`` would scatter one session's fold across N rows.

    Bound (a state_io dispatch is in flight): reads decode the bound row's raw
    JSON once and cache the decoded object for the rest of the dispatch, so a
    handler that reads twice sees one object. :meth:`flush` re-encodes and
    evicts it for the runtime's post-handle CAS-persist.

    Unbound (the CLI, a unit test): there is no bound row, so there is no prior
    state and nothing to persist. Reads miss and writes are DROPPED — see the
    module docstring for why there is deliberately no fallback store. This
    mirrors ``node_delegation_orchestrator``'s split between a bound lookup and
    an unbound path; the difference is that this node has no unbound store to
    fall back to, by ruling.

    Dispatch scoping. ``_cache`` is instance state on a process-wide singleton,
    so it is explicitly scoped to ONE dispatch on two independent axes:

    * The cache records which bound ``rows`` mapping it belongs to and is
      cleared whenever a different one (identity, ``is``) is bound. The runtime
      binds a freshly-built ``{key: (payload_json, version)}`` dict per dispatch
      (``handler_wiring``: ``CONTEXTVAR_STATE_IO_ROWS.set({...})`` … ``reset``),
      so a dispatch can never read an entry another dispatch left behind. This
      is what makes the exception path safe: ``codec.flush`` runs only after
      ``handle()`` returns, so a raising fold abandons its entry — and the next
      dispatch discards it rather than folding on top of it. The previous
      revision guarded only on the payload STRING's identity, which silently
      failed to invalidate whenever no row existed yet for that session (both
      sources being ``None`` compared equal), and let an unbound write be read
      back as prior state by a later dispatch.
    * Each entry additionally records ``(source_payload_json, decoded_state)``,
      so within a dispatch a re-read decodes only when the bound row actually
      changed, and a value THIS dispatch wrote (source ``None``) reads back.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str | None, ModelSessionPhaseState]] = {}
        # Identity of the bound ``rows`` mapping ``_cache`` currently belongs
        # to. ``None`` is a real value here (the unbound shape), so it starts
        # as a sentinel that no ``_read_active_rows()`` result can equal.
        self._cache_rows: object = _UNSCOPED

    def _scoped_cache(
        self, rows: dict[str, tuple[str | None, int]] | None
    ) -> dict[str, tuple[str | None, ModelSessionPhaseState]]:
        """Return ``_cache``, cleared if it belongs to a different dispatch.

        The runtime binds a freshly-built rows mapping per dispatch, so its
        object identity IS the dispatch boundary.
        """
        if self._cache_rows is not rows:
            self._cache.clear()
            self._cache_rows = rows
        return self._cache

    def _lookup(self, session_id: str) -> ModelSessionPhaseState | None:
        """Resolve ``session_id``'s state for the current dispatch, or ``None``.

        Single source of truth shared by ``__getitem__`` and ``__contains__`` so
        the two can never disagree. Unbound, there is no prior state at all.
        Bound, a cached entry counts only when it belongs to THIS dispatch and
        its recorded source string is identical (``is``) to the row currently
        bound for this session — including the ``None`` source of a state THIS
        dispatch just wrote — otherwise the bound raw JSON is decoded and cached.
        """
        rows = _read_active_rows()
        cache = self._scoped_cache(rows)
        if rows is None:
            return None
        payload_json, _version = rows.get(session_id, (None, 0))
        cached = cache.get(session_id)
        if cached is not None and cached[0] is payload_json:
            return cached[1]
        if payload_json is None:
            return None
        decoded = decode(payload_json)
        cache[session_id] = (payload_json, decoded)
        return decoded

    def __getitem__(self, session_id: str) -> ModelSessionPhaseState:
        state = self._lookup(session_id)
        if state is None:
            raise KeyError(session_id)
        return state

    def __setitem__(self, session_id: str, state: ModelSessionPhaseState) -> None:
        rows = _read_active_rows()
        cache = self._scoped_cache(rows)
        if rows is None:
            # Unbound: nothing will read this back and nothing will persist it,
            # and there is no fallback store to put it in — keeping it would be
            # exactly the non-database state of record the OMN-16924 ruling
            # removes, readable as "prior state" by a later unbound fold.
            return
        payload_json, _version = rows.get(session_id, (None, 0))
        cache[session_id] = (payload_json, state)

    def __delitem__(self, session_id: str) -> None:
        del self._scoped_cache(_read_active_rows())[session_id]

    def __contains__(self, session_id: object) -> bool:
        if not isinstance(session_id, str):
            return False
        return self._lookup(session_id) is not None

    def __iter__(self) -> Iterator[str]:
        return iter(self._scoped_cache(_read_active_rows()))

    def __len__(self) -> int:
        return len(self._scoped_cache(_read_active_rows()))

    def flush(self, session_id: str) -> bytes:
        """Re-encode and evict ``session_id``'s state for the CAS-persist step.

        Raises ``KeyError`` when the session was never read or written on this
        proxy during the current dispatch — nothing to persist. The runtime
        calls this INSIDE the ``CONTEXTVAR_STATE_IO_ROWS`` binding
        (``handler_wiring``: ``codec.flush(...)`` precedes ``reset(token)``), so
        the dispatch-scoped cache is still the one ``handle()`` populated.
        """
        _source, state = self._scoped_cache(_read_active_rows()).pop(session_id)
        return encode(state)


_default_proxy: SessionPhaseStateProxy | None = None


def get_default_proxy() -> SessionPhaseStateProxy:
    """Return the process-wide default :class:`SessionPhaseStateProxy`.

    Exactly one ``HandlerSessionPhaseReducer`` is constructed in production (the
    auto-wiring handler resolver instantiates it once at wiring time), so a
    singleton changes no production behavior. It exists so
    :meth:`StateIoCodec.flush` — resolved independently by the runtime from this
    node's contract-declared ``state_io.codec`` reference — reaches the SAME
    per-dispatch cache the handler populated during the just-completed
    ``handle()`` call, without a direct object reference between the two.
    """
    global _default_proxy
    if _default_proxy is None:
        _default_proxy = SessionPhaseStateProxy()
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
    into :class:`SessionPhaseStateProxy` during the just-completed ``handle()``.
    """

    def encode(self, state: ModelSessionPhaseState) -> bytes:
        return encode(state)

    def decode(self, raw: bytes | str) -> ModelSessionPhaseState:
        return decode(raw)

    def flush(self, key: str) -> str | None:
        """Re-encode the proxy's entry for ``key``, if touched this dispatch.

        ``key`` is the contract-declared ``state_io.key`` value for the message
        just dispatched — a ``session_id``, not a correlation_id. Returns
        ``None`` when the session was never read or written on the shared
        default proxy during this dispatch; the runtime then treats the row as
        unchanged and skips persistence.
        """
        try:
            encoded = get_default_proxy().flush(key)
        except KeyError:
            return None
        return encoded.decode("utf-8")


__all__: list[str] = [
    "SessionPhaseStateProxy",
    "StateIoCodec",
    "decode",
    "encode",
    "get_default_proxy",
    "reset_default_proxy",
]
