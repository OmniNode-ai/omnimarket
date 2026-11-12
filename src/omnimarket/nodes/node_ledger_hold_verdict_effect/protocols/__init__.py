# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected dependencies of the ledger hold verdict."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_ledger_hold_verdict_effect.models import (
    ModelHoldVerdictEvent,
)


class ProtocolHoldVerdictEventSink(Protocol):
    def emit(self, event: ModelHoldVerdictEvent) -> None: ...


__all__ = ["ProtocolHoldVerdictEventSink"]
