# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Questions put to the claim index over the rolling work ledger.

The replay, the staleness rule and the refusals are the shared handler ``omnimarket.handlers.claim_index``.
This handler is the node's question-and-answer surface over it. The ledger location is a deployment
fact read from the ``claim_index`` overlay; when it is absent the question is refused naming the key.
It reads the ledger and its rolled archives and, when the overlay names an ``index_path``, writes the
derived cache there and nothing else. It never appends to the ledger.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.handlers import claim_index as index_lib
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.claim_index.model_claim_index_config import ModelClaimIndexConfig
from omnimarket.nodes.node_claim_index_compute.models.model_claim_index import (
    ModelClaimHolder,
    ModelClaimIndexRequest,
    ModelClaimIndexResult,
)

OVERLAY_ID = "claim_index"


def load_claim_index_config(
    environ: Mapping[str, str] | None = None,
) -> ModelClaimIndexConfig:
    """Load ``<root>/claim_index/overlay.yaml``; refuses naming the missing key."""
    return load_structured_overlay(OVERLAY_ID, ModelClaimIndexConfig, environ)


def _holder(ticket: str, record: dict[str, Any], source: str) -> ModelClaimHolder:
    return ModelClaimHolder(
        ticket=ticket,
        lane=str(record["lane"]),
        fence=int(record["fence"]),
        claim_line=int(record["claim_line"]),
        claimed_at=str(record["claimed_at"]),
        last_activity_at=str(record["last_activity_at"]),
        state="stale" if record.get("state") == "stale" else "held",
        source=str(record.get("source") or source),
    )


class HandlerClaimIndex:
    """Answer a holder, live-holders, claim-refusal or push-refusal question over the ledger."""

    def __init__(self, config: ModelClaimIndexConfig | None = None) -> None:
        self._config = config

    def handle(self, request: ModelClaimIndexRequest) -> ModelClaimIndexResult:
        config = self._config if self._config is not None else load_claim_index_config()
        moment = (
            datetime.strptime(request.now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            if request.now
            else datetime.now(UTC)
        )
        ledger = Path(config.ledger_path)
        ledger_name = ledger.name
        if config.index_path is not None:
            index = index_lib.resolve_index(
                ledger, Path(config.index_path), ledger_name, now=moment
            )
        else:
            index = index_lib.build_index_from_sources(
                index_lib.window_sources(ledger, ledger_name, now=moment), now=moment
            )
        sources = [str(entry["name"]) for entry in index.get("sources", [])]
        live_name = str(index.get("source_ledger", ledger_name))

        if request.operation == "holder":
            record = index["tickets"].get(request.ticket)
            return ModelClaimIndexResult(
                operation=request.operation,
                holder=None
                if record is None
                else _holder(request.ticket, record, live_name),
                admitted=True,
                sources=sources,
            )
        if request.operation == "live_holders":
            tickets: dict[str, dict[str, Any]] = index["tickets"]
            live = [
                _holder(ticket, record, live_name)
                for ticket, record in sorted(tickets.items())
                if record.get("state") == "held"
            ]
            return ModelClaimIndexResult(
                operation=request.operation,
                holders=live,
                stale_count=len(tickets) - len(live),
                admitted=True,
                sources=sources,
            )
        if request.operation == "refusal_for_claim":
            refusal = index_lib.refusal_for_claim(
                index, request.ticket, request.lane, now=moment
            )
        else:
            refusal = index_lib.refusal_for_push(
                index, request.ticket, request.lane, fence=request.fence, now=moment
            )
        return ModelClaimIndexResult(
            operation=request.operation,
            refusal=refusal,
            admitted=refusal is None,
            sources=sources,
        )
