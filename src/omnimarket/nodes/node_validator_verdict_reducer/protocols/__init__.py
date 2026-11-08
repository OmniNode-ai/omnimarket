# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected dependency of node_validator_verdict_reducer: where its state lives."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_validator_verdict_reducer.models.model_validator_verdict import (
    ModelVerdictState,
)


@runtime_checkable
class ProtocolVerdictStore(Protocol):
    def load(self) -> ModelVerdictState: ...

    def save(self, state: ModelVerdictState) -> None: ...


__all__ = ["ProtocolVerdictStore"]
