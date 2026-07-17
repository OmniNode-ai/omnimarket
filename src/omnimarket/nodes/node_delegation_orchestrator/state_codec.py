# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Durable-state codec and request-scoped proxy for ``DelegationWorkflowState``
(OMN-14208).

``DelegationWorkflowState`` is a stdlib ``@dataclass`` with nested Pydantic
models, a UUID, an enum, and lists (``handler_delegation_workflow.py:646``) —
it has no ``model_dump()`` / ``model_validate_json()``. ``pydantic.TypeAdapter``
is the correct codec for a plain dataclass built from Pydantic-typed fields;
``dataclasses.asdict()`` or a bare ``json.dumps()`` would not round-trip the
nested Pydantic models, UUID, enum, or datetime fields losslessly.

This module is the sole encode/decode surface a durable ``state_io`` binding
uses to persist and reload workflow state across process boundaries — the
runtime dispatch-seam boundary hook loads state before ``handle()`` and
persists it after, so a workflow's FSM state survives a cold-process replay
instead of living only in the in-process ``_shared_workflows`` dict.

``DelegationWorkflowStateProxy`` is the ``MutableMapping`` that
``HandlerDelegationWorkflow.self._workflows`` proxies to. It is bridged to
``omnibase_infra``'s ``CONTEXTVAR_STATE_IO_ROWS`` (a lazy, ImportError-
tolerant import — see ``_read_active_rows``): the runtime boundary hook binds
a request-scoped ``{correlation_id_str: (payload_json, version)}`` mapping
there before calling ``handle()``. When that ContextVar is unset (every
existing test, standalone/local dispatch, or any caller outside the boundary
hook) the proxy forwards every operation directly to the process-wide
``HandlerDelegationWorkflow._shared_workflows`` ClassVar dict with no local
caching layer — the exact behavior the bare ClassVar default had before this
proxy existed.

``StateIoCodec`` (the class this node's ``contract.yaml`` ``state_io.codec``
declares, ``{module, name}`` per OMN-14208 pair-verify M0) is the bridge
``omnibase_infra``'s wiring calls AFTER ``handle()`` returns: its ``flush(cid)``
re-encodes whatever the proxy's per-request cache holds for that
correlation_id, or returns ``None`` if the proxy never touched it this
dispatch (M1). Infra never reads a write-back value out of
``CONTEXTVAR_STATE_IO_ROWS`` itself — that ContextVar is a load-time input
only.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, MutableMapping
from typing import cast
from uuid import UUID

from pydantic import TypeAdapter

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
)

_ADAPTER: TypeAdapter[DelegationWorkflowState] = TypeAdapter(DelegationWorkflowState)

# omnibase_infra's state_io wiring seam (handler_wiring.py
# _extract_state_io_metadata, OMN-14208) reads a well-known top-level
# `in_flight` JSON key to populate its denormalized `in_flight` column, used
# by staleness sweeps to find abandoned rows. `DelegationWorkflowState`'s
# actual dataclass field is `inference_intent_in_flight` — there is no
# `in_flight` field — so a bare TypeAdapter dump left every persisted row's
# `in_flight` column permanently False (OMN-14208 pair-verify M2). encode()
# injects the derived key; decode() strips it before validating (the
# TypeAdapter has no field of that name to accept it).
_IN_FLIGHT_KEY = "in_flight"


def encode(state: DelegationWorkflowState) -> bytes:
    """Serialize workflow state to JSON bytes for durable storage.

    Injects the well-known top-level ``in_flight`` key derived from
    ``inference_intent_in_flight`` (M2) alongside the TypeAdapter's own
    fields.
    """
    payload = json.loads(_ADAPTER.dump_json(state))
    payload[_IN_FLIGHT_KEY] = state.inference_intent_in_flight
    return json.dumps(payload).encode("utf-8")


def decode(raw: bytes | str) -> DelegationWorkflowState:
    """Deserialize durably-stored JSON back into workflow state.

    Strips the well-known ``in_flight`` key ``encode`` injects before
    validating — it has no corresponding dataclass field.
    """
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload.pop(_IN_FLIGHT_KEY, None)
    return _ADAPTER.validate_json(json.dumps(payload))


def _read_active_rows() -> dict[str, tuple[str | None, int]] | None:
    """Return omnibase_infra's state_io ContextVar value, or ``None``.

    Lazy import: omnimarket's pinned ``omnibase-infra`` rev may predate this
    symbol (the cross-repo OMN-14208 seam ships in ``omnibase_infra`` first —
    the pin at ``pyproject.toml`` is bumped in a separate release step).
    ``ImportError`` degrades to "state_io inactive," exactly like an unset
    ContextVar, so this module never crashes before the paired infra release
    lands; once the new infra is deployed, the import succeeds and the real
    bridge activates with no code change here.
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


class DelegationWorkflowStateProxy(MutableMapping[UUID, DelegationWorkflowState]):
    """``MutableMapping`` view over durable per-request delegation workflow state.

    Two modes, selected per-access by whether omnibase_infra's
    ``CONTEXTVAR_STATE_IO_ROWS`` is bound (never by anything set at
    proxy-construction time, since one long-lived singleton handler instance
    serves every dispatch):

    * **Bound** (a state_io dispatch is in flight): ``__getitem__`` decodes the
      correlation_id's raw JSON exactly once and caches the
      ``DelegationWorkflowState`` on this proxy instance; every subsequent
      read within the same dispatch — including a read that observes an
      in-place attribute mutation the handler made directly on the returned
      object (e.g. the ``inference_intent_in_flight = True`` dedup flag at
      ``handler_delegation_workflow.py:927``, which never calls
      ``__setitem__``) — returns that same cached object. ``flush`` /
      ``flush_all`` re-encode the cached entries for ``StateIoCodec.flush``
      to hand back to the infra-side post-handle persist step, and evict them,
      so a later dispatch for the same correlation_id always decodes the
      freshly-bound raw JSON rather than reusing a stale cached object from a
      previous request.

      **Exception-path staleness (R1, OMN-14208 pair-verify residual):**
      ``codec.flush(cid)`` only runs after ``handle()`` returns
      (``handler_wiring.py``'s ``_load_handle_persist``, inside the ``try``).
      If ``handle()`` raises mid-leg, ``flush`` never runs and this cache
      would otherwise retain the partially-mutated object across the
      ContextVar reset, so a redelivered attempt could decode-hit a stale,
      never-persisted object instead of the freshly-loaded row. The cache
      therefore stores ``(source_payload_json, decoded_state)`` — the exact
      bound-row string this dispatch decoded against — and re-decodes
      whenever the CURRENTLY bound row for ``cid`` is not (``is``, identity)
      that same string object. Every dispatch binds a fresh string (even one
      byte-identical to a prior value — e.g. a re-fetched, unmodified DB row),
      so an abandoned entry from a failed dispatch is mechanically invalidated
      the next time this cid is bound, with no explicit cleanup needed.
    * **Unbound** (tests, standalone/local dispatch, any caller outside the
      boundary hook): every operation forwards directly to
      ``HandlerDelegationWorkflow._shared_workflows`` with no caching layer —
      byte-for-byte the behavior the bare ClassVar default had before this
      proxy existed.
    """

    def __init__(self) -> None:
        # Value is (source_payload_json, decoded_state) — see the
        # exception-path staleness note above (R1).
        self._cache: dict[UUID, tuple[str | None, DelegationWorkflowState]] = {}

    @staticmethod
    def _shared_workflows() -> dict[UUID, DelegationWorkflowState]:
        return HandlerDelegationWorkflow.shared_workflows()

    def _lookup_bound(
        self,
        cid: UUID,
        rows: dict[str, tuple[str | None, int]],
    ) -> DelegationWorkflowState | None:
        """Resolve ``cid``'s state for the CURRENTLY bound dispatch, or ``None``.

        Single source of truth shared by ``__getitem__`` and ``__contains__`` so
        the two can never disagree (OMN-14721). Before this, ``__contains__``
        returned ``True`` whenever ``cid`` was in ``_cache`` while ``__getitem__``
        raised ``KeyError`` for the same ``cid`` when the bound row's payload was
        ``None`` — the exact inconsistency behind the delegation
        routing-intent regression: a leg that CREATED a fresh workflow this
        dispatch (``__setitem__`` caches it under a ``None`` source) saw
        ``cid in self._workflows`` succeed but ``self._workflows[cid]`` blow up,
        so the FSM dedup guard could not read back a workflow it had just
        created and the leg committed an empty outbox batch.

        A cached entry counts ONLY when its recorded source string is identical
        (``is``) to the row currently bound for ``cid`` — including the ``None``
        source of a workflow THIS dispatch just created — otherwise the bound raw
        JSON is decoded (and cached), or ``None`` is returned when no row exists
        yet. This keys the guard on the durable per-leg row plus this dispatch's
        own writes, never on a stale object an earlier, unflushed dispatch left
        behind (the R1 exception-path staleness invariant is preserved: a
        different bound source string re-decodes).
        """
        payload_json, _version = rows.get(str(cid), (None, 0))
        cached = self._cache.get(cid)
        if cached is not None and cached[0] is payload_json:
            return cached[1]
        if payload_json is None:
            return None
        decoded = decode(payload_json)
        self._cache[cid] = (payload_json, decoded)
        return decoded

    def __getitem__(self, cid: UUID) -> DelegationWorkflowState:
        rows = _read_active_rows()
        if rows is None:
            return self._shared_workflows()[cid]
        state = self._lookup_bound(cid, rows)
        if state is None:
            raise KeyError(cid)
        return state

    def __setitem__(self, cid: UUID, state: DelegationWorkflowState) -> None:
        rows = _read_active_rows()
        if rows is None:
            self._shared_workflows()[cid] = state
            return
        payload_json, _version = rows.get(str(cid), (None, 0))
        self._cache[cid] = (payload_json, state)

    def __delitem__(self, cid: UUID) -> None:
        # Never exercised on the live dispatch path: no site in
        # handler_delegation_workflow.py deletes/pops a workflow entry
        # (verified by grep, OMN-14208) — provided only for MutableMapping
        # interface completeness.
        if _read_active_rows() is None:
            del self._shared_workflows()[cid]
            return
        del self._cache[cid]

    def __contains__(self, cid: object) -> bool:
        rows = _read_active_rows()
        if rows is None:
            return cid in self._shared_workflows()
        if not isinstance(cid, UUID):
            return False
        # OMN-14721: mirror ``_lookup_bound``'s presence predicate EXACTLY
        # (without its decode/cache side effect) so ``cid in proxy`` is true iff
        # ``proxy[cid]`` would succeed. A cache entry counts only when its
        # recorded source ``is`` the currently bound row (incl. the ``None``
        # source of a just-created workflow); a stale cache entry from an
        # earlier, unflushed dispatch (source mismatch, ``None`` bound row) is
        # NOT present — it must re-decode on the next read.
        payload_json, _version = rows.get(str(cid), (None, 0))
        cached = self._cache.get(cid)
        if cached is not None and cached[0] is payload_json:
            return True
        return payload_json is not None

    def __iter__(self) -> Iterator[UUID]:
        if _read_active_rows() is None:
            return iter(self._shared_workflows())
        return iter(self._cache)

    def __len__(self) -> int:
        if _read_active_rows() is None:
            return len(self._shared_workflows())
        return len(self._cache)

    def flush(self, cid: UUID) -> bytes:
        """Re-encode ``cid``'s cached state for the post-handle CAS-persist step.

        Evicts the entry from this proxy's cache afterward, so a later
        dispatch for the same correlation_id decodes the freshly-bound raw
        JSON instead of reusing this request's cached object. Raises
        ``KeyError`` if ``cid`` was never loaded or set on this proxy
        instance during the current dispatch — nothing to persist.
        """
        _source, state = self._cache.pop(cid)
        return encode(state)

    def flush_all(self) -> dict[UUID, bytes]:
        """Re-encode and evict every entry touched (loaded or set) this dispatch."""
        flushed = {cid: encode(state) for cid, (_source, state) in self._cache.items()}
        self._cache.clear()
        return flushed


_default_proxy: DelegationWorkflowStateProxy | None = None


def get_default_proxy() -> DelegationWorkflowStateProxy:
    """Return the process-wide default ``DelegationWorkflowStateProxy``.

    Exactly one ``HandlerDelegationWorkflow`` is constructed in production —
    the auto-wiring handler resolver instantiates it once at wiring time (see
    ``HandlerDelegationWorkflow.__init__``), so sharing this singleton changes
    no production behavior. It exists so ``StateIoCodec.flush`` — resolved
    independently by ``omnibase_infra.runtime.auto_wiring.handler_wiring``
    from this node's contract-declared ``state_io.codec`` reference — can
    reach the SAME decoded-object cache the handler's own default-constructed
    proxy populated during the just-completed ``handle()`` call, without a
    direct object reference between the two independently-resolved
    instances (OMN-14208 pair-verify M1).
    """
    global _default_proxy
    if _default_proxy is None:
        _default_proxy = DelegationWorkflowStateProxy()
    return _default_proxy


class StateIoCodec:
    """Contract-declared state_io codec bridging the runtime dispatch seam.

    Resolved independently by ``omnibase_infra.runtime.auto_wiring.
    handler_wiring`` from this node's ``contract.yaml`` ``state_io.codec``
    ``{module, name}`` reference (OMN-14208 pair-verify M0) and instantiated
    once at wiring time. ``encode``/``decode`` delegate to the module-level
    functions above; ``flush`` is the explicit post-handle bridge infra calls
    to read back whatever ``DelegationWorkflowStateProxy`` decoded and
    mutated during the just-completed ``handle()`` call (M1) — infra no
    longer expects the proxy to write its result back into
    ``CONTEXTVAR_STATE_IO_ROWS`` itself.
    """

    def encode(self, state: DelegationWorkflowState) -> bytes:
        return encode(state)

    def decode(self, raw: bytes | str) -> DelegationWorkflowState:
        return decode(raw)

    def flush(self, cid: str) -> str | None:
        """Re-encode the proxy's cached entry for ``cid``, if touched this dispatch.

        ``DelegationWorkflowStateProxy.flush`` already re-encodes (returns
        JSON bytes, not a ``DelegationWorkflowState``) — this only decodes
        those bytes to the ``str`` infra's wiring expects.

        Returns ``None`` when ``cid`` was never loaded or set on the shared
        default proxy during the current dispatch — the caller then treats
        the row as unchanged and skips persistence.
        """
        try:
            encoded = get_default_proxy().flush(UUID(cid))
        except KeyError:
            return None
        return encoded.decode("utf-8")


__all__: list[str] = [
    "DelegationWorkflowStateProxy",
    "StateIoCodec",
    "decode",
    "encode",
    "get_default_proxy",
]
