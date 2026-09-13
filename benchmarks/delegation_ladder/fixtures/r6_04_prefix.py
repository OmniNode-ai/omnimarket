class LongLivedTerminalCorrelator:
    """Matches terminal events to awaiting callers by correlation id.

    ``_deliver`` is driven by an always-on poll loop that runs whether or not a
    caller is waiting. ``_wait`` is called by the caller, which may attach after
    the terminal has already arrived.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._pending: dict[str, asyncio.Future[dict[str, object]]] = {}

    def _deliver(self, correlation_id: str, body: dict[str, object]) -> None:
        future = self._pending.pop(correlation_id, None)
        if future is None:
            return
        if not future.done():
            future.set_result(body)

    async def _wait(
        self, correlation_id: str, timeout_seconds: float
    ) -> dict[str, object] | None:
        future = self._pending.get(correlation_id)
        if future is None:
            future = self._loop.create_future()
            self._pending[correlation_id] = future
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=timeout_seconds
            )
        except TimeoutError:
            self._pending.pop(correlation_id, None)
            return None
