from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelOrchestratorStartCommand:
    orchestrator_id: str
    initial_phase: str
    started_by: Optional[str] = None
    metadata: Optional[dict] = None