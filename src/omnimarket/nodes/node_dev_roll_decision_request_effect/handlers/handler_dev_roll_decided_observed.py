# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decided-observed handler of node_dev_roll_decision_request_effect (OMN-24251).

One decision of node_dev_roll_request_compute in, one observation record out.
A ``roll_now`` decision clears every pending event it rolled and becomes the
shadow last roll; a hold (or any other action) changes nothing. Never raises
out of ``handle``: a resolution or store error comes back as ``recorded=False``
with the message in ``error`` (a :class:`NodeOverlayError` is a
``ValueError``). The handler holds no producer: the runtime publishes the
returned model on the contract's ``published_events`` topic through its
event_bus.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from uuid import UUID

from omnimarket.models.dev_roll import (
    EnumDevRollAction,
    ModelDevRollDecided,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers.store_dev_roll_observation import (
    store_from_policy,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.models import (
    ModelDevRollDecisionRequestPolicy,
    ModelDevRollObservationRecorded,
    ModelDevRollObservationState,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.overlay import (
    load_decision_request_policy,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.protocols import (
    ProtocolDevRollObservationStore,
)


class HandlerDevRollDecidedObserved:
    """``handle(request) -> ModelDevRollObservationRecorded``.

    The runtime builds the handler with no arguments; the policy and store are
    then resolved lazily on the first request and cached on the instance. A
    test passes them in.
    """

    def __init__(
        self,
        store: ProtocolDevRollObservationStore | None = None,
        *,
        policy: ModelDevRollDecisionRequestPolicy | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._environ = environ

    def _resolve_store(self) -> ProtocolDevRollObservationStore:
        if self._store is None:
            environ = os.environ if self._environ is None else self._environ
            if self._policy is None:
                self._policy = load_decision_request_policy(environ)
            self._store = store_from_policy(self._policy, environ)
        return self._store

    def _record(
        self,
        request: ModelDevRollDecided,
        cleared_count: int,
        recorded: bool,
        error: str | None,
    ) -> ModelDevRollObservationRecorded:
        return ModelDevRollObservationRecorded(
            correlation_id=request.correlation_id,
            action=str(request.action),
            cleared_count=cleared_count,
            recorded=recorded,
            error=error,
        )

    def handle(self, request: ModelDevRollDecided) -> ModelDevRollObservationRecorded:
        """One decision in, one observation record out; a hold changes nothing."""
        would_request = request.would_request
        if request.action is not EnumDevRollAction.ROLL_NOW or would_request is None:
            return self._record(request, 0, True, None)
        try:
            store = self._resolve_store()
            state = store.load()
            rolled: frozenset[UUID] = frozenset(request.rolled_event_ids)
            pending = tuple(
                event for event in state.pending_events if event.event_id not in rolled
            )
            store.save(
                ModelDevRollObservationState(
                    pending_events=pending,
                    seen_job_ids=state.seen_job_ids,
                    last_roll_job_id=str(request.correlation_id),
                    last_roll_git_ref=would_request.git_ref,
                    last_roll_started_at=request.observed_at,
                )
            )
        except (ValueError, OSError) as exc:
            return self._record(request, 0, False, str(exc))
        return self._record(
            request, len(state.pending_events) - len(pending), True, None
        )
