# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shape of one HTTP exchange a store adapter is handed (OMN-20944).

The node's contract declares its HTTP transport, and the node's routed handler
owns it: an adapter never opens a connection itself. It is given an exchange
and calls it as ``await exchange(method, url, params=..., payload=...,
headers=..., timeout=...)``, getting back the status and the parsed JSON object
(empty when the body is not one). A transport failure raises
``ConnectionError`` naming only the exception type.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: ``(method, url, *, params, payload, headers, timeout) -> (status, json object)``.
HttpExchange = Callable[..., Awaitable[tuple[int, dict[str, Any]]]]

__all__ = ["HttpExchange"]
