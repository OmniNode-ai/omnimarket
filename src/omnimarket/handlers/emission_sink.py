# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where emission events go (OMN-19998, 17a): in memory by default, an append-only JSON-lines journal as the real backend.

The journal is the durable record the receipts are read from. Publishing the same events on the bus topics the
emission compute node declares is the runtime's job once the node is attached; this sink does not publish.

Shared by every node that emits under the floor's emission contract (OMN-22566): the floor's own cycle and the
read-only principal's probe write to the same journal, so one receipt fold counts both. ``ChainEmitter`` is the
contract for one chain: a start, then a success or a typed failure, all under one correlation id.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError

from omnimarket.models.floor_17a import ModelNodeEmission

# The floor's join fold reads a failure with no start as a refusal only with this prefix.
UPSTREAM_PREFIX = "upstream_"


class ProtocolEmissionSink(Protocol):
    def emit(self, event: ModelNodeEmission) -> None: ...

    def read(self, since: datetime) -> tuple[ModelNodeEmission, ...]: ...


class InMemoryEmissionSink:
    def __init__(self) -> None:
        self.events: list[ModelNodeEmission] = []

    def emit(self, event: ModelNodeEmission) -> None:
        self.events.append(event)

    def read(self, since: datetime) -> tuple[ModelNodeEmission, ...]:
        return tuple(e for e in self.events if e.emitted_at >= since)


class JsonlEmissionSink:
    """Append-only journal. A line that does not parse is skipped and counted, never silently fixed."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.skipped_lines = 0

    def emit(self, event: ModelNodeEmission) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = event.model_dump_json() + "\n"
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def read(self, since: datetime) -> tuple[ModelNodeEmission, ...]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return ()
        events: list[ModelNodeEmission] = []
        self.skipped_lines = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                event = ModelNodeEmission.model_validate(json.loads(line))
            except (ValueError, ValidationError):
                self.skipped_lines += 1
                continue
            if event.emitted_at >= since:
                events.append(event)
        return tuple(events)


class ChainEmitter:
    """One chain under the emission contract: ``start``, then ``success`` or ``failure``, under one correlation id.

    The emitter refuses to emit out of order (a terminal before the start, a second terminal, a second start), so a
    caller that breaks the contract fails here and does not write a chain the receipt fold would name as broken.
    """

    def __init__(
        self,
        sink: ProtocolEmissionSink,
        *,
        node: str,
        piece: int,
        clock: Callable[[], datetime] | None = None,
        correlation_id: str | None = None,
        backend: str = "real",
        drill: bool = False,
    ) -> None:
        self._sink = sink
        self._node = node
        self._piece = piece
        self._clock = clock or (lambda: datetime.now(UTC))
        self.correlation_id = correlation_id or uuid4().hex
        self._backend = backend
        self._drill = drill
        self._started = False
        self._closed = False

    @property
    def open(self) -> bool:
        """True from the start until a success or a failure has been emitted."""
        return self._started and not self._closed

    def start(self) -> None:
        if self._started:
            raise RuntimeError("a chain starts once")
        self._started = True
        self._emit("start")

    def success(self, counters: dict[str, int] | None = None) -> None:
        self._close()
        self._emit("success", counters=counters or {})

    def failure(self, code: str, detail: str = "") -> None:
        self._close()
        self._emit("failure", failure_code=code, failure_detail=detail[:300])

    def refuse(self, code: str, detail: str = "") -> None:
        """A typed refusal: a failure that never started, the shape of a join's error case.

        The receipt fold reads it as a refusal only when ``code`` carries the upstream prefix, and only when the
        chain never started, so a started chain or any other code is refused here.
        """
        if self._started:
            raise RuntimeError("a refusal is a failure that never started")
        if not code.startswith(UPSTREAM_PREFIX):
            raise ValueError(
                f"a refusal carries a code starting with {UPSTREAM_PREFIX!r}"
            )
        self._started = True
        self._closed = True
        self._emit("failure", failure_code=code, failure_detail=detail[:300])

    def _close(self) -> None:
        if not self._started:
            raise RuntimeError("a chain ends only after its start")
        if self._closed:
            raise RuntimeError("a chain ends once")
        self._closed = True

    def _emit(self, phase: str, **extra: object) -> None:
        self._sink.emit(
            ModelNodeEmission.model_validate(
                {
                    "node": self._node,
                    "piece": self._piece,
                    "correlation_id": self.correlation_id,
                    "phase": phase,
                    "emitted_at": self._clock(),
                    "backend": self._backend,
                    "drill": self._drill,
                    **extra,
                }
            )
        )
