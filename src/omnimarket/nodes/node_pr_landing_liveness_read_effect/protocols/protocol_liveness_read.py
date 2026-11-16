# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The liveness read node's ports: the reader and the event publisher (OMN-23529)."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class ProtocolLivenessReader(Protocol):
    def read(self, repository: str, head_sha: str) -> tuple[str, ...]:
        """Repository paths of the undeclared installer sources the head adds; raise LivenessReadError when the read
        cannot be made."""
        ...


class ProtocolLivenessReadPublisher(Protocol):
    def publish(self, topic: str, event: BaseModel) -> None: ...


__all__ = ["ProtocolLivenessReadPublisher", "ProtocolLivenessReader"]
