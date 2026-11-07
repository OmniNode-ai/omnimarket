# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Append per-seam reading rows to the injected store; a rewrite of a stored row is refused (OMN-22909)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.contract_topics import contract_section
from omnimarket.nodes.node_seam_reading_compute.models.model_seam_row import (
    EnumRowAppendStatus,
    ModelSeamRowAppendRequest,
    ModelSeamRowAppendResult,
)
from omnimarket.nodes.node_seam_reading_compute.protocols import ProtocolSeamRowStore

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    topics = contract_section(path, "topics")
    if not isinstance(topics, dict):
        raise ValueError(f"{path}: topics must be a mapping")
    return {str(role): str(topic) for role, topic in topics.items()}


class HandlerSeamRowAppend:
    """Append every row of a request; report each row's status, refusing none silently."""

    def __init__(self, *, store: ProtocolSeamRowStore) -> None:
        self._store = store

    def handle(self, request: ModelSeamRowAppendRequest) -> ModelSeamRowAppendResult:
        statuses = tuple(self._store.append(row) for row in request.rows)
        refused = tuple(
            row.key
            for row, status in zip(request.rows, statuses, strict=True)
            if status is EnumRowAppendStatus.REFUSED_REWRITE
        )
        return ModelSeamRowAppendResult(
            correlation_id=request.correlation_id,
            statuses=statuses,
            appended=statuses.count(EnumRowAppendStatus.APPENDED),
            already_appended=statuses.count(EnumRowAppendStatus.ALREADY_APPENDED),
            refused=len(refused),
            refused_keys=refused,
        )


def answer_command(
    payload: bytes, handler: HandlerSeamRowAppend, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One append request in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a request, and a request with a refused rewrite, each end in the typed
    refused event; nothing is dropped.
    """
    try:
        request = ModelSeamRowAppendRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["seam_rows_refused"], json.dumps(failure).encode())]
    result = handler.handle(request)
    body: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "appended": result.appended,
        "already_appended": result.already_appended,
        "refused": result.refused,
    }
    started = (
        topics["seam_rows_append_started"],
        json.dumps({"correlation_id": result.correlation_id}).encode(),
    )
    if result.refused:
        body["failure"] = f"rewrite-refused:{','.join(result.refused_keys)}"
        return [
            started,
            (topics["seam_rows_refused"], json.dumps(body, sort_keys=True).encode()),
        ]
    return [
        started,
        (topics["seam_rows_appended"], json.dumps(body, sort_keys=True).encode()),
    ]


__all__ = ["HandlerSeamRowAppend", "answer_command", "load_topics"]
