# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#!/usr/bin/env python3

"""
This module handles the ticket pipeline orchestration.
It manages the lifecycle of ticket processing tasks including start,
completion, and phase transitions.
"""

from .handlers import HandlerTicketPipeline
from .models import (
    ModelPipelineCompletedEvent,
    ModelPipelinePhaseEvent,
    ModelPipelineStartCommand,
    ModelPipelineState,
)

__all__ = [
    "NodeTicketPipeline",
    "HandlerTicketPipeline",
    "ModelPipelineCompletedEvent",
    "ModelPipelinePhaseEvent",
    "ModelPipelineStartCommand",
    "ModelPipelineState",
]


class NodeTicketPipeline(HandlerTicketPipeline):
    """ONEX entry-point wrapper for HandlerTicketPipeline."""
