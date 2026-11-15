# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected dependencies of the ruling register read."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_ruling_register import (
    ModelRulingReading,
    ModelRulingRow,
)
from omnimarket.nodes.node_ruling_register_effect.models import (
    ModelRulingReadEvent,
)


class ProtocolRulingReadPort(Protocol):
    """One row's read: the reading its command saw on its host, never a raise.

    A port bounds its read by the lesser of its own timeout and ``budget_seconds``
    when the caller hands it one.
    """

    def read(
        self, row: ModelRulingRow, budget_seconds: int | None = None
    ) -> ModelRulingReading: ...


class ProtocolRulingEventSink(Protocol):
    def emit(self, event: ModelRulingReadEvent) -> None: ...


__all__ = ["ProtocolRulingEventSink", "ProtocolRulingReadPort"]
