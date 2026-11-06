# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipted classifier delegation port; transport and model selection belong to DI."""

from typing import Any, Protocol

from omnimarket.models.model_orchestrator_promise import ModelPromiseDelegateResult
from omnimarket.models.operator_capture import ModelUtterance


class ProtocolPromiseDelegate(Protocol):
    def classify(
        self, utterance: ModelUtterance, response_contract: dict[str, Any]
    ) -> ModelPromiseDelegateResult: ...
