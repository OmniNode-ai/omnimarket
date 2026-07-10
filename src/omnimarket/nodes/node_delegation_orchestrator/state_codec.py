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
``HandlerDelegationWorkflow.self._workflows`` proxies to. It is
ContextVar-backed: the runtime boundary hook binds a request-scoped raw JSON
mapping via ``bind_state_context`` before calling ``handle()``, then reads
back the proxy's touched entries via ``flush`` / ``flush_all`` for the
post-handle CAS-persist step. When the ContextVar is unset (every existing
test, standalone/local dispatch, or any caller outside the boundary hook) the
proxy forwards every operation directly to the process-wide
``HandlerDelegationWorkflow._shared_workflows`` ClassVar dict with no local
caching layer — the exact behavior the bare ClassVar default had before this
proxy existed.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

from pydantic import TypeAdapter

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
)

_ADAPTER: TypeAdapter[DelegationWorkflowState] = TypeAdapter(DelegationWorkflowState)


def encode(state: DelegationWorkflowState) -> bytes:
    """Serialize workflow state to JSON bytes for durable storage."""
    return _ADAPTER.dump_json(state)


def decode(raw: bytes | str) -> DelegationWorkflowState:
    """Deserialize durably-stored JSON back into workflow state."""
    return _ADAPTER.validate_json(raw)


# Request-scoped raw JSON mapping, keyed by correlation_id. ``None`` (the
# default) means no state_io boundary hook is active for the current
# execution context — every proxy falls back to `_shared_workflows`.
_DELEGATION_STATE_CONTEXT: ContextVar[Mapping[UUID, bytes] | None] = ContextVar(
    "delegation_workflow_state_context", default=None
)


@contextmanager
def bind_state_context(raw_mapping: Mapping[UUID, bytes]) -> Iterator[None]:
    """Bind ``raw_mapping`` as the request-scoped state context for one dispatch.

    The state_io runtime boundary hook (omnibase_infra) wraps each dispatch:
    SELECT the row(s) by correlation_id, bind the raw JSON here, call
    ``handle()``, then read back ``DelegationWorkflowStateProxy.flush`` /
    ``flush_all`` for the CAS-persist step. Always unset outside an active
    dispatch (the ContextVar resets to ``None`` on exit), so a caller that
    never binds this context gets the pre-OMN-14208 ``_shared_workflows``
    behavior automatically.
    """
    token = _DELEGATION_STATE_CONTEXT.set(raw_mapping)
    try:
        yield
    finally:
        _DELEGATION_STATE_CONTEXT.reset(token)


class DelegationWorkflowStateProxy(MutableMapping[UUID, DelegationWorkflowState]):
    """``MutableMapping`` view over durable per-request delegation workflow state.

    Two modes, selected per-access by whether ``_DELEGATION_STATE_CONTEXT`` is
    bound (never by anything set at proxy-construction time, since one
    long-lived singleton handler instance serves every dispatch):

    * **Bound** (a state_io dispatch is in flight): ``__getitem__`` decodes the
      correlation_id's raw JSON exactly once and caches the
      ``DelegationWorkflowState`` on this proxy instance; every subsequent
      read within the same dispatch — including a read that observes an
      in-place attribute mutation the handler made directly on the returned
      object (e.g. the ``inference_intent_in_flight = True`` dedup flag at
      ``handler_delegation_workflow.py:927``, which never calls
      ``__setitem__``) — returns that same cached object. ``flush`` /
      ``flush_all`` re-encode the cached entries for the post-handle
      CAS-persist step and evict them, so a later dispatch for the same
      correlation_id always decodes the freshly-bound raw JSON rather than
      reusing a stale cached object from a previous request.
    * **Unbound** (tests, standalone/local dispatch, any caller outside the
      boundary hook): every operation forwards directly to
      ``HandlerDelegationWorkflow._shared_workflows`` with no caching layer —
      byte-for-byte the behavior the bare ClassVar default had before this
      proxy existed.
    """

    def __init__(self) -> None:
        self._cache: dict[UUID, DelegationWorkflowState] = {}

    @staticmethod
    def _shared_workflows() -> dict[UUID, DelegationWorkflowState]:
        return HandlerDelegationWorkflow.shared_workflows()

    def __getitem__(self, cid: UUID) -> DelegationWorkflowState:
        raw_mapping = _DELEGATION_STATE_CONTEXT.get()
        if raw_mapping is None:
            return self._shared_workflows()[cid]
        if cid not in self._cache:
            self._cache[cid] = decode(raw_mapping[cid])
        return self._cache[cid]

    def __setitem__(self, cid: UUID, state: DelegationWorkflowState) -> None:
        if _DELEGATION_STATE_CONTEXT.get() is None:
            self._shared_workflows()[cid] = state
            return
        self._cache[cid] = state

    def __delitem__(self, cid: UUID) -> None:
        # Never exercised on the live dispatch path: no site in
        # handler_delegation_workflow.py deletes/pops a workflow entry
        # (verified by grep, OMN-14208) — provided only for MutableMapping
        # interface completeness.
        if _DELEGATION_STATE_CONTEXT.get() is None:
            del self._shared_workflows()[cid]
            return
        del self._cache[cid]

    def __contains__(self, cid: object) -> bool:
        raw_mapping = _DELEGATION_STATE_CONTEXT.get()
        if raw_mapping is None:
            return cid in self._shared_workflows()
        return cid in self._cache or cid in raw_mapping

    def __iter__(self) -> Iterator[UUID]:
        if _DELEGATION_STATE_CONTEXT.get() is None:
            return iter(self._shared_workflows())
        return iter(self._cache)

    def __len__(self) -> int:
        if _DELEGATION_STATE_CONTEXT.get() is None:
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
        return encode(self._cache.pop(cid))

    def flush_all(self) -> dict[UUID, bytes]:
        """Re-encode and evict every entry touched (loaded or set) this dispatch."""
        flushed = {cid: encode(state) for cid, state in self._cache.items()}
        self._cache.clear()
        return flushed


__all__: list[str] = [
    "DelegationWorkflowStateProxy",
    "bind_state_context",
    "decode",
    "encode",
]
