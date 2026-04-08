from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelOrchestratorCompletedEvent:
    orchestrator_id: str
    status: str
    completed_at: Optional[str] = None
    error_message: Optional[str] = None