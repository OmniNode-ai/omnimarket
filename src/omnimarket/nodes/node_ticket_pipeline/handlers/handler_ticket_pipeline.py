# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from ..models.model_pipeline_completed_event import ModelPipelineCompletedEvent
from ..models.model_pipeline_phase_event import ModelPipelinePhaseEvent
from ..models.model_pipeline_start_command import ModelPipelineStartCommand
from ..models.model_pipeline_state import ModelPipelineState


class HandlerTicketPipeline:
    def __init__(self):
        self.state = {}

    def handle_start(
        self, command: ModelPipelineStartCommand
    ) -> ModelPipelinePhaseEvent:
        self.state[command.pipeline_id] = ModelPipelineState(
            pipeline_id=command.pipeline_id,
            current_phase=command.phase,
            status="started",
        )
        return ModelPipelinePhaseEvent(
            pipeline_id=command.pipeline_id,
            phase=command.phase,
            event_type="phase_started",
        )

    def handle_complete(
        self, event: ModelPipelineCompletedEvent
    ) -> ModelPipelinePhaseEvent:
        if event.pipeline_id in self.state:
            self.state[event.pipeline_id].status = event.status
            self.state[event.pipeline_id].last_updated = event.completed_at
        return ModelPipelinePhaseEvent(
            pipeline_id=event.pipeline_id,
            phase=self.state[event.pipeline_id].current_phase,
            event_type="phase_completed",
            details={"status": event.status},
        )
