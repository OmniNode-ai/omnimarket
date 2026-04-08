from ..models import (
    PipelineStartCommand,
    PipelineState,
    PipelinePhaseEvent,
    PipelineCompletedEvent
)


class TicketPipelineHandler:
    def __init__(self):
        self.state = PipelineState(pipeline_id='default')

    def handle_start(self, command: PipelineStartCommand) -> PipelinePhaseEvent:
        self.state.current_phase = command.phase
        self.state.status = 'running'
        return PipelinePhaseEvent(
            pipeline_id=command.pipeline_id,
            phase=command.phase
        )

    def handle_complete(self, event: PipelineCompletedEvent) -> None:
        self.state.status = 'completed'
        self.state.current_phase = None