from dataclasses import dataclass
from typing import Optional

@dataclass
class OrchestratorCompletedEvent:
    orchestrator_id: str
    status: str
    completed_at: Optional[str] = None