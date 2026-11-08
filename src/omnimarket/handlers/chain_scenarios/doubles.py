# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory dependency doubles a node's scenario bindings name (OMN-19998).

A factory takes the run's ``BindingContext`` and returns the dependency. Every double
counts the calls it receives, so a bare retry is visible to the assertions, and a
reducer-state double records the writes it takes so a skipped update is visible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox


@dataclass(frozen=True)
class BindingContext:
    """What a binding factory may use: a scratch directory private to the run."""

    tmp_dir: Path


class _RecordingPublishAdapter:
    def __init__(self) -> None:
        self.received: list[tuple[str, dict[str, Any]]] = []

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        assert isinstance(payload, dict)
        self.received.append((topic, json.loads(json.dumps(payload))))


class SpoolEmitter:
    """The real emit-effect with an isolated spool and a recording publish adapter."""

    def __init__(self, ctx: BindingContext) -> None:
        self.calls = 0
        self.published = _RecordingPublishAdapter()
        self._inner = HandlerEventEmitEffect(
            spool=SpoolOutbox(ctx.tmp_dir / "spool"), publish_adapter=self.published
        )

    def handle(self, request: ModelEmitRequest) -> ModelEmitResult:
        self.calls += 1
        return self._inner.handle(request)


class UnavailableEmitter:
    """The emit-effect spool is down: every call raises."""

    def __init__(self, _ctx: BindingContext) -> None:
        self.calls = 0

    def handle(self, request: ModelEmitRequest) -> ModelEmitResult:
        self.calls += 1
        raise ConnectionError("spool unavailable")


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Connection:
    def __init__(self, db: RecordingLedgerDb) -> None:
        self._db = db

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, sql: str, *args: Any) -> None:
        await self._db.execute(sql, *args)


class _Acquire:
    def __init__(self, db: RecordingLedgerDb) -> None:
        self._db = db

    async def __aenter__(self) -> _Connection:
        return _Connection(self._db)

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, db: RecordingLedgerDb) -> None:
        self._db = db

    def acquire(self) -> _Acquire:
        return _Acquire(self._db)


class RecordingLedgerDb:
    """Reducer-state double for the work-ledger projection writer's database."""

    _TARGETS = ("work_ledger_rows", "work_ledger_state")

    def __init__(self, _ctx: BindingContext) -> None:
        self.calls = 0
        self.skip_writes = False
        self.connected = False
        self.writes: list[tuple[str, str]] = []

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls += 1
        if self.skip_writes:
            return
        for target in self._TARGETS:
            if target in sql:
                self.writes.append((target, str(args[0])))

    @property
    def pool(self) -> _Pool:
        return _Pool(self)


def spool_emitter(ctx: BindingContext) -> SpoolEmitter:
    return SpoolEmitter(ctx)


def unavailable_emitter(ctx: BindingContext) -> UnavailableEmitter:
    return UnavailableEmitter(ctx)


def recording_ledger_db(ctx: BindingContext) -> RecordingLedgerDb:
    return RecordingLedgerDb(ctx)


__all__ = [
    "BindingContext",
    "RecordingLedgerDb",
    "SpoolEmitter",
    "UnavailableEmitter",
    "recording_ledger_db",
    "spool_emitter",
    "unavailable_emitter",
]
