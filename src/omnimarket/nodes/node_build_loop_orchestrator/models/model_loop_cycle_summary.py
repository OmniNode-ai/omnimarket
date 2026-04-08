from dataclasses import dataclass
from typing import Optional

@dataclass
class LoopCycleSummary:
    cycle_id: str
    phase: str
    status: str
    duration: Optional[float] = None