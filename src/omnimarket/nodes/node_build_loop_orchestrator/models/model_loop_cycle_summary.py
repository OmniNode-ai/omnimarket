from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelLoopCycleSummary:
    orchestrator_id: str
    cycle_number: int
    phases_executed: list
    status: str
    completed_at: Optional[str] = None
    error_details: Optional[str] = None