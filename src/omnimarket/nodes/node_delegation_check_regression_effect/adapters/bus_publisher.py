# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish the node's typed payloads, wrapped in the platform envelope, to a contract-declared topic on the lab bus."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus

BusKind = Literal["kafka", "inmemory"]


class BusRegressionPublisher:
    """One bus opened for the scan and closed after it; a bus that cannot be opened or written is an OSError."""

    def __init__(self, bus: BusKind, lane: str | None, workspace: Path | None) -> None:
        self._bus_kind = bus
        self._lane = lane if bus == "kafka" else None
        self._workspace = workspace
        self._loop = asyncio.new_event_loop()
        # One context for every call: the bus opener sets a context variable on enter that its exit resets, and each
        # run_until_complete otherwise runs in a context of its own.
        self._context = contextvars.copy_context()
        self._cm: contextlib.AbstractAsyncContextManager[ProtocolLabRunBus] | None = (
            None
        )
        self._bus: ProtocolLabRunBus | None = None

    def _run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        return self._loop.run_until_complete(
            self._loop.create_task(coro, context=self._context)
        )

    def _open(self) -> ProtocolLabRunBus:
        if self._bus is None:
            # The opener's fourth parameter carries the workspace root; it is bound by position in declared order, as
            # the gateway read client binds it, so this module names no registry directory.
            names = tuple(inspect.signature(open_lab_run_bus).parameters)
            if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
                raise OSError(f"unexpected bus opener parameters: {names}")
            values: tuple[Any, ...] = (
                self._bus_kind,
                self._lane,
                None,
                self._workspace,
            )
            opener: Callable[
                ..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]
            ] = open_lab_run_bus
            self._cm = opener(**dict(zip(names, values, strict=True)))
            try:
                self._bus = self._run(self._cm.__aenter__())
            except Exception as exc:
                raise OSError(
                    f"bus open: {type(exc).__name__}: {str(exc)[:160]}"
                ) from exc
        return self._bus

    def publish(self, topic: str, key: str, payload: BaseModel) -> None:
        bus = self._open()
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=payload.model_dump(mode="json"),
            correlation_id=uuid.uuid5(uuid.NAMESPACE_URL, key),
            event_type=event_type_for(topic),
        )
        try:
            self._run(
                bus.publish(
                    topic,
                    key.encode("utf-8"),
                    envelope.model_dump_json().encode("utf-8"),
                )
            )
        except Exception as exc:
            raise OSError(
                f"publish {topic}: {type(exc).__name__}: {str(exc)[:160]}"
            ) from exc

    def close(self) -> None:
        try:
            if self._cm is not None:
                self._run(self._cm.__aexit__(None, None, None))
        finally:
            self._loop.close()

    def __enter__(self) -> BusRegressionPublisher:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
