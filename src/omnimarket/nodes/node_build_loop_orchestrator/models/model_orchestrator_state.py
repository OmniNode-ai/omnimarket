from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelOrchestratorState:
    orchestrator_id: str
    current_phase: Optional[str] = None
    status: Optional[str] = None
    last_updated: Optional[str] = None