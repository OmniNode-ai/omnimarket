from dataclasses import dataclass
from typing import Optional

@dataclass
class OrchestratorState:
    orchestrator_id: str
    current_phase: Optional[str] = None
    status: str = 'idle'