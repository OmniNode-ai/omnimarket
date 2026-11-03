# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read Phoenix's trace count and newest trace time for the factory floor (OMN-22097)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from omnibase_infra.errors import InfraUnavailableError
from omnibase_infra.gateway.client.gateway_transport_httpx import GatewayTransportHttpx

from omnimarket.models.model_floor_phoenix_traces import ModelFloorPhoenixTraces

_QUERY = "{ projects { edges { node { name traceCount endTime } } } }"
_TIMEOUT_SECONDS = 10


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unread(now: datetime, detail: str) -> ModelFloorPhoenixTraces:
    return ModelFloorPhoenixTraces(state="unread", read_at=_stamp(now), detail=detail)


async def _post(url: str) -> str:
    """Read through the infrastructure transport, which owns the HTTP client lifecycle."""
    transport = GatewayTransportHttpx(timeout_seconds=_TIMEOUT_SECONDS)
    response = await transport.post_json(
        f"{url.rstrip('/')}/graphql",
        body=json.dumps({"query": _QUERY}),
        headers={"Content-Type": "application/json"},
    )
    if not 200 <= response.status < 300:
        raise ValueError("the Phoenix HTTP response was not successful")
    return await response.text()


def load_phoenix_traces(url: str, now: datetime) -> ModelFloorPhoenixTraces | None:
    """The trace count over every project and the age of the newest trace; None when no URL is configured.

    A read that fails is ``unread`` with the exception's class name: the detail goes to a published page, so it
    never carries the address.
    """
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        return _unread(now, "the Phoenix URL is not http or https")
    try:
        document = json.loads(asyncio.run(_post(url)))
        edges = document["data"]["projects"]["edges"]
        nodes = [edge["node"] for edge in edges]
        count = sum(int(node["traceCount"]) for node in nodes)
        ends = [
            datetime.fromisoformat(node["endTime"]) for node in nodes if node["endTime"]
        ]
    except (OSError, InfraUnavailableError, ValueError, KeyError, TypeError) as exc:
        return _unread(now, f"the Phoenix read failed: {type(exc).__name__}")
    if not ends:
        return ModelFloorPhoenixTraces(
            state="read",
            trace_count=count,
            read_at=_stamp(now),
            detail="no project holds a trace end time",
        )
    newest = max(ends)
    return ModelFloorPhoenixTraces(
        state="read",
        trace_count=count,
        newest_span_at=_stamp(newest),
        newest_span_age_seconds=max(0, int((now - newest).total_seconds())),
        read_at=_stamp(now),
    )
