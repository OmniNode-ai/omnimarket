# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay boundaries for the shared claim store and canonical dispatch service."""

from typing import Protocol, runtime_checkable

from omnimarket.models.item_state_check import (
    ModelItemClaim,
    ModelItemDelivery,
    ModelItemDispatchReceipt,
    ModelItemRecoveryPlan,
)


@runtime_checkable
class ProtocolItemRecoveryStore(Protocol):
    def claim(self, plan: ModelItemRecoveryPlan) -> tuple[bool, ModelItemClaim]: ...
    def latest(self, family: str) -> ModelItemClaim | None: ...
    def settle(
        self, plan: ModelItemRecoveryPlan, receipt: ModelItemDispatchReceipt
    ) -> None: ...
    def delivery(self, family: str) -> ModelItemDelivery | None: ...
    def notice(self, family: str, delivery: ModelItemDelivery) -> None: ...


@runtime_checkable
class ProtocolItemDispatch(Protocol):
    def dispatch(self, plan: ModelItemRecoveryPlan) -> ModelItemDispatchReceipt:
        """Start or deliver once under plan.cause, returning the durable owner/delivery acknowledgement."""
        ...

    def print_line(self, line: str) -> bool: ...
