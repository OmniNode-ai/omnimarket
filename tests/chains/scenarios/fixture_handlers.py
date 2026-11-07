# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixture handlers and dependency doubles for the chain scenario runner's own tests."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict

from omnimarket.chain_scenarios.doubles import BindingContext


class ModelFixtureRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row: str
    correlation_id: str = ""


class ModelFixtureResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    accepted: bool
    reason: str = ""


class Sink:
    def __init__(self, _ctx: BindingContext) -> None:
        self.calls = 0

    async def write(self, row: str) -> None:
        self.calls += 1


class DownSink(Sink):
    async def write(self, row: str) -> None:
        self.calls += 1
        raise ConnectionError("sink down")


class SlowSink(Sink):
    async def write(self, row: str) -> None:
        self.calls += 1
        await asyncio.sleep(0.5)


class Store:
    def __init__(self, _ctx: BindingContext) -> None:
        self.calls = 0
        self.skip_writes = False
        self.writes: list[tuple[str, str]] = []

    def put(self, key: str) -> None:
        self.calls += 1
        if not self.skip_writes:
            self.writes.append(("rows", key))


def sink(ctx: BindingContext) -> Sink:
    return Sink(ctx)


def down_sink(ctx: BindingContext) -> DownSink:
    return DownSink(ctx)


def slow_sink(ctx: BindingContext) -> SlowSink:
    return SlowSink(ctx)


def store(ctx: BindingContext) -> Store:
    return Store(ctx)


class HandlerTyped:
    """Reports a sink failure as a typed result, once."""

    def __init__(self, sink: Sink, store: Store | None = None) -> None:
        self._sink = sink
        self._store = store

    async def handle(self, request: ModelFixtureRequest) -> ModelFixtureResult:
        try:
            await self._sink.write(request.row)
        except ConnectionError:
            return ModelFixtureResult(
                correlation_id=request.correlation_id,
                accepted=False,
                reason="sink_down",
            )
        if self._store is not None:
            self._store.put(request.row)
        return ModelFixtureResult(correlation_id=request.correlation_id, accepted=True)


class HandlerBareRetry(HandlerTyped):
    """Retries a down sink three times before it reports the failure."""

    async def handle(self, request: ModelFixtureRequest) -> ModelFixtureResult:
        for _attempt in range(3):
            try:
                await self._sink.write(request.row)
                return ModelFixtureResult(
                    correlation_id=request.correlation_id, accepted=True
                )
            except ConnectionError:
                continue
        return ModelFixtureResult(
            correlation_id=request.correlation_id, accepted=False, reason="sink_down"
        )


class HandlerRaises(HandlerTyped):
    """Lets the sink error escape: the adapter reports no event at all."""

    async def handle(self, request: ModelFixtureRequest) -> ModelFixtureResult:
        await self._sink.write(request.row)
        return ModelFixtureResult(correlation_id=request.correlation_id, accepted=True)
