# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two reads behind the J10 join, so a test or a deployment binds its own backend (OMN-22957)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_dev_deploy_reading_compute.models.model_join_j10 import (
    ModelReceiptRead,
    ModelRuntimeAnswer,
    ModelRuntimeAsk,
)


@runtime_checkable
class ProtocolLabPassReceiptReader(Protocol):
    """Every lab pass receipt found for one exact sha; an unreadable store is reported, never an empty list."""

    def read_receipts(self, sha: str) -> ModelReceiptRead: ...


@runtime_checkable
class ProtocolRuntimeProbe(Protocol):
    """Sends one correlated functional request to the running runtime and reports what it answered."""

    def ask(self, ask: ModelRuntimeAsk) -> ModelRuntimeAnswer: ...


__all__: list[str] = ["ProtocolLabPassReceiptReader", "ProtocolRuntimeProbe"]
