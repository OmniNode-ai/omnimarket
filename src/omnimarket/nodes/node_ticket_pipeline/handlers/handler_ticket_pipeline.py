# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from omnimarket.nodes.node_ticket_pipeline.models import (
    ModelPipelineCompletedEvent,
    ModelPipelinePhaseEvent,
    ModelPipelineStartCommand,
)


class HandlerTicketPipeline:
    def __init__(self):
        self.state = None

    def handle_start_command(
        self, command: ModelPipelineStartCommand
    ) -> ModelPipelinePhaseEvent | None:
        # Placeholder implementation
        return None

    def handle_phase_event(
        self, event: ModelPipelinePhaseEvent
    ) -> ModelPipelineCompletedEvent | None:
        # Placeholder implementation
        return None

    def handle_completed_event(self, event: ModelPipelineCompletedEvent) -> None:
        # Placeholder implementation
        pass
