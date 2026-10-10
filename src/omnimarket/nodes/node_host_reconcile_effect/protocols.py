# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected evaluation and publication boundaries for host reconciliation."""

from typing import Protocol

from pydantic import BaseModel

from omnimarket.models.model_host_reconcile import (
    ModelHostReconcileDecisions,
    ModelHostReconcileEvaluateRequest,
)


class ProtocolHostReconcileEvaluator(Protocol):
    def handle(
        self, request: ModelHostReconcileEvaluateRequest
    ) -> ModelHostReconcileDecisions: ...


class ProtocolHostReconcileEventPublisher(Protocol):
    def publish(self, topic: str, event: BaseModel) -> None: ...
