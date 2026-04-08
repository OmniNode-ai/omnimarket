from dataclasses import dataclass
from typing import Optional

@dataclass
class PipelinePhaseEvent:
    pipeline_id: str
    phase: str
    timestamp: Optional[str] = None