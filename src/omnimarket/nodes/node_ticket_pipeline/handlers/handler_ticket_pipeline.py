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

    def handle_start_command(self, command: ModelPipelineStartCommand):
        # Logic for handling start command
        pass

    def handle_phase_event(self, event: ModelPipelinePhaseEvent):
        # Logic for handling phase events
        pass

    def handle_completed_event(self, event: ModelPipelineCompletedEvent):
        # Logic for handling completed events
        pass
