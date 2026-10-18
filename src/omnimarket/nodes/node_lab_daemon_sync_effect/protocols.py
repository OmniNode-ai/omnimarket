# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected host and publication boundaries for the lab daemon sync."""

from typing import Protocol

from pydantic import BaseModel


class ProtocolLabDaemonHost(Protocol):
    def run(self, argv: list[str], timeout_s: int) -> tuple[int, str, str]: ...

    def read_json(self, path: str) -> dict[str, object] | None: ...

    def lock_busy(self, path: str) -> bool: ...

    def write_text(self, path: str, text: str) -> None: ...

    def exists(self, path: str) -> bool: ...


class ProtocolLabDaemonEventPublisher(Protocol):
    def publish(self, topic: str, event: BaseModel) -> None: ...
