# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
import threading
from collections.abc import Callable
from typing import Any


class HandlerThreadPoster:
    def __init__(self, handler_func: Callable[[Any], None]) -> None:
        self.handler_func = handler_func
        self._lock = threading.Lock()
        self._queue: list[Any] = []
        self._thread = threading.Thread(target=self._process_queue, daemon=True)
        self._thread.start()

    def post(self, event: Any) -> None:
        with self._lock:
            self._queue.append(event)

    def _process_queue(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    continue
                event = self._queue.pop(0)
            self.handler_func(event)
