# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read Phoenix's trace count and newest trace time for the factory floor (OMN-22097)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.error import URLError
from urllib.request import Request, urlopen

from omnimarket.models.model_floor_phoenix_traces import ModelFloorPhoenixTraces

_QUERY = "{ projects { edges { node { name traceCount endTime } } } }"
_TIMEOUT_SECONDS = 10


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unread(now: datetime, detail: str) -> ModelFloorPhoenixTraces:
    return ModelFloorPhoenixTraces(state="unread", read_at=_stamp(now), detail=detail)


def _post(url: str) -> bytes:
    request = Request(
        f"{url.rstrip('/')}/graphql",
        data=json.dumps({"query": _QUERY}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        body: bytes = response.read()
    return body


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
        document = json.loads(_post(url))
        edges = document["data"]["projects"]["edges"]
        nodes = [edge["node"] for edge in edges]
        count = sum(int(node["traceCount"]) for node in nodes)
        ends = [
            datetime.fromisoformat(node["endTime"]) for node in nodes if node["endTime"]
        ]
    except (OSError, URLError, ValueError, KeyError, TypeError) as exc:
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
