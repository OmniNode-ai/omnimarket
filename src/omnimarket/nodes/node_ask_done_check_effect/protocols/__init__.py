# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected dependencies of the ask done-check node. The ledger read and append ports are the
reading writer's (ProtocolLedgerRowSource, ProtocolReadingAppender)."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ModelFloorRead(BaseModel):
    """The floor's pieces by number with their status, and when the floor was generated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pieces: dict[int, str] = {}
    generated_at: str | None = None
    error: str | None = None


class ProtocolFloorRead(Protocol):
    def read(self) -> ModelFloorRead: ...


__all__ = ["ModelFloorRead", "ProtocolFloorRead"]
