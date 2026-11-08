# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate store's backend interface: one transaction per request (plan section 7.5)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_gate_store import (
    ModelGateStoreRequest,
    ModelGateStoreResult,
)


@runtime_checkable
class ProtocolGateStore(Protocol):
    """One serializable store; a request either commits whole or leaves no trace."""

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        """Apply one request in one transaction and return its typed answer."""
        ...
