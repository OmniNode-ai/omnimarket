from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelOrchestratorResult:
    orchestrator_id: str
    success: bool
    result_data: Optional[dict] = None
    error_message: Optional[str] = None