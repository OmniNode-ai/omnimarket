# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer's in-memory state store, its default backend (OMN-23078).

The reducer is pure: the file-backed store, which does I/O, lives with the effects in
node_validator_workflow_orchestrator.adapters.verdict_store.
"""

from __future__ import annotations

from omnimarket.models.model_validator_verdict import (
    ModelVerdictState,
)


class InMemoryVerdictStore:
    def __init__(self, state: ModelVerdictState | None = None) -> None:
        self._state = state or ModelVerdictState()

    def load(self) -> ModelVerdictState:
        return self._state

    def save(self, state: ModelVerdictState) -> None:
        self._state = state
