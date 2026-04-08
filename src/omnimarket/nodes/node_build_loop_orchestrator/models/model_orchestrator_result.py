from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelOrchestratorResult:
    orchestrator_id: str
    result_data: Optional[dict] = None
    processed_at: Optional[str] = None