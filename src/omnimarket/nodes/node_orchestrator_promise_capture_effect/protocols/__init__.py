# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dependency boundaries for promise capture."""

from typing import Protocol

from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseCaptureRecord,
    ModelPromiseClassification,
    ModelPromiseClassifyRequest,
    ModelPromiseStoredCapture,
)
from omnimarket.models.operator_capture import ModelUtterance


class ProtocolPromiseClassifier(Protocol):
    def handle(
        self, request: ModelPromiseClassifyRequest
    ) -> ModelPromiseClassification: ...


class ProtocolPromiseStore(Protocol):
    def assemble(
        self, record: ModelPromiseCaptureRecord
    ) -> tuple[str, ModelUtterance | None]: ...
    def get(self, capture_id: str) -> ModelPromiseStoredCapture | None: ...
    def put(self, capture: ModelPromiseStoredCapture) -> bool: ...
    def captures(self) -> tuple[ModelPromiseStoredCapture, ...]: ...
