# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the gateway publishes workflow command and event messages."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class WorkflowPublishError(RuntimeError):
    """The broker refused or lost a publish."""


@runtime_checkable
class ProtocolWorkflowPublisher(Protocol):
    """Publish a wire message to a broker topic."""

    async def publish(
        self,
        *,
        topic: str,
        key: str,
        wire: dict[str, Any],
    ) -> None:
        """Publish one wire message; raises WorkflowPublishError on every failure path; returning means the broker accepted it."""
        ...


__all__: list[str] = [
    "ProtocolWorkflowPublisher",
    "WorkflowPublishError",
]
