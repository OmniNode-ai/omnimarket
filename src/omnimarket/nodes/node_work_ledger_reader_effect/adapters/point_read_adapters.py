# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read routes and event sinks of the moved ledger reader: in-memory and real backend."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    ModelCommandReadRoute,
    ModelInMemoryReadRoute,
    ModelPointAnswer,
    ModelPointQuery,
    ModelReaderEvent,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)


class ReaderRouteError(RuntimeError):
    """The overlay names a route this host cannot build."""


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _request(query: ModelPointQuery, correlation_id: str) -> dict[str, object]:
    request: dict[str, object] = {
        "correlation_id": correlation_id,
        "query": query.query,
    }
    if query.lanes:
        request["lanes"] = list(query.lanes)
    if query.lane:
        request["lane"] = query.lane
    if query.since is not None:
        request["since"] = _stamp(query.since)
    if query.kinds:
        request["kinds"] = list(query.kinds)
    if query.limit is not None:
        request["limit"] = query.limit
    if query.page_size is not None:
        request["page_size"] = query.page_size
    if query.cursor is not None:
        request["cursor"] = query.cursor
    return request


def answer_from_json(
    answer: Mapping[str, Any], correlation_id: str
) -> ModelPointAnswer:
    """The query node's typed answer, reduced to what a reader uses. Never raises."""
    if answer.get("correlation_id") != correlation_id:
        return ModelPointAnswer(error="answer-for-another-request")
    if answer.get("error") or answer.get("status", "ok") != "ok":
        return ModelPointAnswer(error=f"node-error:{str(answer.get('error'))[:200]}")
    lines = answer.get("lines") or ()
    if not isinstance(lines, list | tuple) or not all(
        isinstance(x, str) for x in lines
    ):
        return ModelPointAnswer(error="answer-lines-malformed")
    freshness = answer.get("freshness")
    projected = (
        _when(freshness.get("newest_projected_at"))
        if isinstance(freshness, Mapping)
        else None
    )
    parity = answer.get("parity")
    matched = answer.get("matched", 0)
    cursor = answer.get("next_cursor")
    try:
        watermark = _when(answer.get("watermark"))
    except ValueError:
        return ModelPointAnswer(error="answer-watermark-malformed")
    return ModelPointAnswer(
        lines=tuple(lines),
        matched=matched if isinstance(matched, int) else 0,
        # A rows answer has no point-query watermark; its freshness stands in for it.
        watermark=watermark or projected,
        newest_projected_at=projected,
        parity=str(parity.get("status"))
        if isinstance(parity, Mapping) and parity.get("status")
        else None,
        next_cursor=cursor if isinstance(cursor, str) and cursor else None,
    )


class CommandPointRead:
    """The real backend: one typed request on stdin of the bus read command, one JSON answer."""

    def __init__(
        self,
        argv: Sequence[str],
        timeout_seconds: int,
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._argv = list(argv)
        self._timeout = timeout_seconds
        self._new_id = new_id

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        correlation_id = self._new_id()
        try:
            done = subprocess.run(
                self._argv,
                input=json.dumps(_request(query, correlation_id)),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ModelPointAnswer(error=f"read-command-failed:{type(exc).__name__}")
        if done.returncode != 0:
            return ModelPointAnswer(error=f"read-command-exit-{done.returncode}")
        for line in reversed(done.stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return answer_from_json(parsed, correlation_id)
        return ModelPointAnswer(error="read-answer-not-json")


class InMemoryPointRead:
    """The in-memory default: a fixed answer per query kind, and a log of every request."""

    def __init__(self, answers: Mapping[str, ModelPointAnswer] | None = None) -> None:
        self._answers = dict(answers or {})
        self.requests: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.requests.append(query)
        return self._answers.get(
            query.query, ModelPointAnswer(error=f"no-inmemory-answer-for-{query.query}")
        )


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[ModelReaderEvent] = []

    def emit(self, event: ModelReaderEvent) -> None:
        self.events.append(event)


class JournalEventSink:
    """Appends each event as one JSON line; the floor and a reading read the journal."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def emit(self, event: ModelReaderEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")


def build_point_read(
    route: ModelCommandReadRoute | ModelInMemoryReadRoute,
    environ: Mapping[str, str] | None = None,
) -> ProtocolLedgerPointRead:
    if isinstance(route, ModelInMemoryReadRoute):
        return InMemoryPointRead()
    env = os.environ if environ is None else environ
    argv = shlex.split(env.get(route.command_env, ""))
    if not argv:
        raise ReaderRouteError(
            f"{route.command_env} is not set; the overlay names it as the bus read command"
        )
    return CommandPointRead(argv, route.timeout_seconds)


__all__ = [
    "CommandPointRead",
    "InMemoryEventSink",
    "InMemoryPointRead",
    "JournalEventSink",
    "ReaderRouteError",
    "answer_from_json",
    "build_point_read",
]
