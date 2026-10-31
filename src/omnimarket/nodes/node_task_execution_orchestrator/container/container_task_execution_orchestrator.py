# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler factories for node_task_execution_orchestrator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnimarket.nodes.node_task_execution_orchestrator.handlers.handler_task_execution_orchestrator import (
    HandlerTaskExecutionOrchestrator,
)
from omnimarket.nodes.node_verification_receipt_generator.handlers.handler_verification_receipt import (
    HandlerVerificationReceiptGenerator,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
        HandlerDelegateSkill,
    )

__all__ = [
    "create_handler_delegate_skill",
    "create_handler_task_execution_orchestrator",
    "create_handler_verification_receipt_generator",
]


def create_handler_delegate_skill() -> HandlerDelegateSkill:
    """Build the delegation handler, loading its dependencies only on demand."""
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
        HandlerDelegateSkill,
    )

    return HandlerDelegateSkill()


def create_handler_task_execution_orchestrator() -> HandlerTaskExecutionOrchestrator:
    """Build the task planner with its default dependencies."""
    return HandlerTaskExecutionOrchestrator()


def create_handler_verification_receipt_generator() -> (
    HandlerVerificationReceiptGenerator
):
    """Build the mechanical-check execution handler."""
    return HandlerVerificationReceiptGenerator()
