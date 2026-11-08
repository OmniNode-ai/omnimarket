# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate store effect: ``handle(ModelGateStoreRequest) -> ModelGateStoreResult``.

The backend is injected; the in-memory twin is the default and the ``gate_store`` overlay
selects it. The Postgres backend is not built yet: the overlay naming it is refused with a
typed error, never answered by the twin. ``answer_command`` is the bus edge: one command in,
the typed event it produces out.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    ModelGateStoreFailure,
    ModelGateStorePolicy,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore

CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"


class GateStoreBackendNotBuiltError(RuntimeError):
    """The overlay selected a backend that is not built; nothing answers in its place."""


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def build_store(policy: ModelGateStorePolicy) -> ProtocolGateStore:
    """The backend the overlay names; refuses one that is not built."""
    if policy.backend is EnumGateStoreBackend.IN_MEMORY:
        return InMemoryGateStore(policy)
    raise GateStoreBackendNotBuiltError(
        f"gate store backend {policy.backend.value} is not built"
    )


class HandlerGateStore:
    """One request, one transaction on the injected store."""

    def __init__(self, store: ProtocolGateStore) -> None:
        self._store = store

    def handle(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        """Apply the request in one transaction and return the typed answer."""
        return self._store.transact(request)


def answer_command(
    payload: bytes, handler: HandlerGateStore, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a request, or a store that cannot answer, is answered with a typed
    failure event naming the failure, never dropped and never retried silently.
    """
    try:
        request = ModelGateStoreRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = ModelGateStoreFailure(
            correlation_id="unknown", failure=f"bad-payload:{fields}"
        )
        return [(topics["gate_store_failed"], failure.model_dump_json().encode())]
    try:
        result = handler.handle(request)
    except Exception as exc:  # the store's own fault is an answer, not a crash
        failure = ModelGateStoreFailure(
            correlation_id=request.correlation_id,
            failure=f"store-fault:{type(exc).__name__}",
        )
        return [(topics["gate_store_failed"], failure.model_dump_json().encode())]
    return [
        (
            topics["gate_store_answered"],
            json.dumps(result.model_dump(mode="json"), sort_keys=True).encode(),
        )
    ]
