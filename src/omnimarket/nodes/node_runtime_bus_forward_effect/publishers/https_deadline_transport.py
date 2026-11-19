# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Apply a cancellable wall-clock deadline beneath the synchronous HTTPX client."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx


def run_sync[T](operation: Callable[[], T]) -> T:
    """Run the synchronous publisher seam even when core calls it from a running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="https-ingest"
        ) as worker:
            return worker.submit(operation).result()
    return operation()


class DeadlineTransport(httpx.BaseTransport):
    """Drive HTTPX's asynchronous I/O and close it before leaving a flush.

    A synchronous socket read can block again after each incoming chunk. Here the
    deadline cancels the exchange through the response body and lets HTTPX close
    the interrupted socket before the sender starts another attempt.
    """

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport
        self._loop = asyncio.new_event_loop()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._loop.run_until_complete(self._exchange(request))

    async def _exchange(self, request: httpx.Request) -> httpx.Response:
        deadline: float = request.extensions["attempt_deadline"]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("attempt expired before send")
        async with asyncio.timeout(remaining):
            response = await self._transport.handle_async_request(request)
            try:
                body = await response.aread()
                # The sender consumes the decoded body. Drop the headers that
                # described its original encoding and transfer framing.
                return httpx.Response(
                    response.status_code,
                    headers=[
                        (name, value)
                        for name, value in response.headers.raw
                        if name.lower()
                        not in (
                            b"content-encoding",
                            b"content-length",
                            b"transfer-encoding",
                        )
                    ],
                    content=body,
                    extensions=response.extensions,
                )
            finally:
                await response.aclose()

    def close(self) -> None:
        try:
            self._loop.run_until_complete(self._transport.aclose())
        finally:
            # Closing the loop does not wait for an OS DNS lookup that was
            # cancelled from the request's point of view.
            self._loop.close()
