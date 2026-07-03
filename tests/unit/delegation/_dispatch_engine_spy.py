# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed recording spy over ``MessageDispatchEngine`` for delegation wiring tests.

This is a real ``MessageDispatchEngine`` subclass whose two ``register_*`` methods
record the calls the delegation wiring makes instead of mutating engine state. It
is a typed contract-level spy, NOT a faked inference/routing boundary: the subject
under test is the wiring's registration behaviour (which dispatchers/routes it
registers), so a bare ``MagicMock`` assigned onto the dispatch surface (OMN-13501
no-faked-boundary) is replaced with this real, typed collaborator. Inheriting the
concrete engine satisfies the ``engine: MessageDispatchEngine`` parameter type
through real inheritance while the recorded ``unittest.mock.call`` objects preserve
the ``.args`` / ``.kwargs`` inspection the assertions rely on.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import call

from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

__all__ = ["RecordingDispatchEngine"]


class RecordingDispatchEngine(MessageDispatchEngine):  # type: ignore[misc]
    """Real dispatch engine that records ``register_dispatcher`` / ``register_route``."""

    def __init__(self) -> None:
        super().__init__()
        self.dispatcher_calls: list[Any] = []
        self.route_calls: list[Any] = []

    def register_dispatcher(self, *args: Any, **kwargs: Any) -> None:
        self.dispatcher_calls.append(call(*args, **kwargs))

    def register_route(self, *args: Any, **kwargs: Any) -> None:
        self.route_calls.append(call(*args, **kwargs))
