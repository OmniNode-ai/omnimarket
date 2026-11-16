# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory ports of the liveness read node: a recorded read and a publisher that keeps what it was given (OMN-23529)."""

from __future__ import annotations

from pydantic import BaseModel

from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.liveness_read_errors import (
    LivenessReadError,
)


class InMemoryLivenessReader:
    """ProtocolLivenessReader over reads recorded per (repository, head)."""

    def __init__(
        self,
        found: dict[tuple[str, str], tuple[str, ...]],
        unreadable: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._found = dict(found)
        self._unreadable = dict(unreadable or {})

    def read(self, repository: str, head_sha: str) -> tuple[str, ...]:
        key = (repository, head_sha)
        if key in self._unreadable:
            raise LivenessReadError(self._unreadable[key])
        if key not in self._found:
            raise LivenessReadError(
                f"no liveness read recorded for {repository}@{head_sha}"
            )
        return self._found[key]


class InMemoryLivenessReadPublisher:
    """ProtocolLivenessReadPublisher that keeps each (topic, event)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        self.published.append((topic, event))


__all__ = ["InMemoryLivenessReadPublisher", "InMemoryLivenessReader"]
