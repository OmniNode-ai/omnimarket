from dataclasses import dataclass

@dataclass
class OrchestratorStartCommand:
    orchestrator_id: str
    phase: str