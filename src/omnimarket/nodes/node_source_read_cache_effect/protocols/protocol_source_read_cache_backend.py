# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The protocol a source read cache backend implements."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class SourceCacheUnavailableError(RuntimeError):
    """The one error a backend raises when it cannot be reached."""


@runtime_checkable
class ProtocolSourceReadCacheBackend(Protocol):
    def get(self, key: str) -> str | None: ...

    def put(self, key: str, value: str, ttl_seconds: int) -> None: ...

    def close(self) -> None: ...
