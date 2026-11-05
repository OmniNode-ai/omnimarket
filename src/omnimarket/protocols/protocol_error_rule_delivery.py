# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected ports; delivery never loads a table or launches a matcher process."""

from typing import Protocol

from omnimarket.models.error_rule_delivery import (
    ModelDeliveryAlarmRequest,
    ModelDeliveryAlarmResult,
    ModelDeliveryFailure,
    ModelDeliveryMatch,
)
from omnimarket.models.exception_queue import (
    ModelExceptionEmitRequest,
    ModelExceptionEmitResult,
)


class ProtocolDeliveryMatcher(Protocol):
    def handle(self, request: ModelDeliveryFailure) -> ModelDeliveryMatch: ...


class ProtocolDeliveryPath(Protocol):
    def deliver(self, failure: ModelDeliveryFailure) -> None: ...


class ProtocolDeliveryAlarm(Protocol):
    def handle(
        self, request: ModelDeliveryAlarmRequest
    ) -> ModelDeliveryAlarmResult: ...


class ProtocolDeliveryExceptionQueue(Protocol):
    def handle(
        self, request: ModelExceptionEmitRequest
    ) -> ModelExceptionEmitResult: ...
