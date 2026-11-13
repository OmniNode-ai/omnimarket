# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus session read port: every page of a window inside one reusable connection (OMN-23977).

``CommandPointRead`` spawns one ``onex ledger query --stdin`` process per page; each page paid
the process start and the Kafka connect again (measured on the dev bus 2026-11-12: about 3.7 s
and 5 s against 0.3 to 1.7 s of node time). The infra query client holds one connection open
across queries when it is entered as an async context manager; this port enters it once, on the
first read, and answers every page of the window from it. The handler closes the port after one
read (``ProtocolClosable``), so a session never outlives the read that opened it.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from types import TracebackType
from typing import Any, Protocol

from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    _request,
    answer_from_json,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    ModelPointAnswer,
    ModelPointQuery,
)


class ProtocolBusQueryClient(Protocol):
    """The infra query client: an async context manager whose ``query`` is one request."""

    async def __aenter__(self) -> ProtocolBusQueryClient: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def query(
        self, request: dict[str, Any], *, timeout_s: float
    ) -> dict[str, Any]: ...


class BusSessionPointRead:
    """Reads each page through one open session; an error is an answer, never a raise."""

    def __init__(
        self,
        client_factory: Callable[[], ProtocolBusQueryClient],
        timeout_seconds: int,
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._client_factory = client_factory
        self._timeout = timeout_seconds
        self._new_id = new_id
        self._runner: asyncio.Runner | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._client: ProtocolBusQueryClient | None = None
        #: How many sessions this port has opened; a window read opens exactly one.
        self.sessions_opened = 0

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        correlation_id = self._new_id()
        request = _request(query, correlation_id)
        try:
            runner, client = self._session()
            answer = runner.run(client.query(request, timeout_s=self._timeout))
        except (
            Exception
        ) as exc:  # boundary: a failed page is an error answer, never a raise
            self.close()
            return ModelPointAnswer(
                error=f"bus-session-failed:{type(exc).__name__}:{str(exc)[:160]}"
            )
        return answer_from_json(answer, correlation_id)

    def _session(self) -> tuple[asyncio.Runner, ProtocolBusQueryClient]:
        if self._runner is not None and self._client is not None:
            return self._runner, self._client
        self._runner = asyncio.Runner()
        self._stack = contextlib.AsyncExitStack()
        self._client = self._runner.run(
            self._stack.enter_async_context(self._client_factory())
        )
        self.sessions_opened += 1
        return self._runner, self._client

    def close(self) -> None:
        """Close the session; safe to call twice and when never opened; never raises."""
        runner, stack = self._runner, self._stack
        self._runner = self._stack = self._client = None
        with contextlib.suppress(Exception):
            if runner is not None and stack is not None:
                runner.run(stack.aclose())
        with contextlib.suppress(Exception):
            if runner is not None:
                runner.close()


__all__ = ["BusSessionPointRead", "ProtocolBusQueryClient"]
