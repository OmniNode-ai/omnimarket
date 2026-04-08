from ..models import (
    OrchestratorStartCommand,
    OrchestratorState,
    OrchestratorCompletedEvent,
    OrchestratorResult
)


class OrchestratorHandler:
    def __init__(self):
        self.state = OrchestratorState(orchestrator_id='default')

    def handle_start(self, command: OrchestratorStartCommand) -> OrchestratorResult:
        self.state.current_phase = command.phase
        self.state.status = 'running'
        return OrchestratorResult(success=True, message='Started')

    def handle_complete(self, event: OrchestratorCompletedEvent) -> None:
        self.state.status = 'completed'
        self.state.current_phase = None