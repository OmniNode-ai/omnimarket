# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer one floor route from the projection's held document (OMN-23072, slice 1).

The routes and the refusals come from the node's contract; this handler holds neither a path nor a message.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from omnimarket.handlers.floor_document_store import get_floor_document_store
from omnimarket.nodes.node_floor_page_api_effect.models.model_floor_page_request import (
    ModelFloorPageRequest,
    ModelFloorPageResponse,
    ModelFloorRefusal,
    ModelFloorRoute,
)
from omnimarket.protocols.protocol_floor_document_store import (
    ProtocolFloorDocumentStore,
)

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"


@lru_cache(maxsize=1)
def _contract() -> dict[str, object]:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{_CONTRACT} must hold a mapping")
    return raw


def contract_routes() -> tuple[ModelFloorRoute, ...]:
    return tuple(ModelFloorRoute.model_validate(r) for r in _contract()["floor_routes"])  # type: ignore[attr-defined]


def contract_mount_prefix() -> str:
    mount = _contract()["http_mount"]
    return str(mount["path_prefix"])  # type: ignore[index]


def contract_refusal(name: str) -> ModelFloorRefusal:
    return ModelFloorRefusal.model_validate(_contract()["refusals"][name])  # type: ignore[index]


def _refusal_response(name: str) -> ModelFloorPageResponse:
    refusal = contract_refusal(name)
    return ModelFloorPageResponse(
        status_code=refusal.status,
        body={"code": refusal.code, "detail": refusal.detail},
    )


class HandlerFloorPage:
    """Reads the held floor document and returns the declared document's body."""

    def __init__(self, store: ProtocolFloorDocumentStore | None = None) -> None:
        self._store = store

    def handle(self, request: ModelFloorPageRequest) -> ModelFloorPageResponse:
        if request.document not in {r.document for r in contract_routes()}:
            return _refusal_response("unknown_document")
        held = (self._store or get_floor_document_store()).latest()
        if held is None:
            return _refusal_response("not_projected")
        return ModelFloorPageResponse(
            status_code=200,
            body=held.document.model_dump(mode="json"),
            computed_at=held.computed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
