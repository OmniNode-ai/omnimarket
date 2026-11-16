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
    """One row's read: the reading its command saw on its host, never a raise."""

    def read(self, row: ModelRulingRow) -> ModelRulingReading: ...


class ProtocolRulingEventSink(Protocol):
    def emit(self, event: ModelRulingReadEvent) -> None: ...


__all__ = ["ProtocolRulingEventSink", "ProtocolRulingReadPort"]
