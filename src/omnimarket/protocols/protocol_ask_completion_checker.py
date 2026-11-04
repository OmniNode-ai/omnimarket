# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared read-only completion evidence boundary for the report and transition nodes."""

from typing import Protocol

from omnimarket.models.model_ask_state import (
    ModelAskCompletionCheck,
    ModelAskStateRequest,
)
from omnimarket.models.model_ask_state_transition import ModelAskStateTransition


class ProtocolAskCompletionChecker(Protocol):
    def checks(
        self,
        snapshot: ModelAskStateRequest,
        transitions: tuple[ModelAskStateTransition, ...] = (),
    ) -> tuple[ModelAskCompletionCheck, ...]: ...
