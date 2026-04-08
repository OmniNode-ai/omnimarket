from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelLoopCycleSummary:
    orchestrator_id: str
    cycle_number: int
    status: str
    summary_time: Optional[str] = None
    details: Optional[str] = None