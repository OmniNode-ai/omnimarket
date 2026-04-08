from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelPipelinePhaseEvent:
    pipeline_id: str
    phase_name: str
    status: str
    timestamp: Optional[str] = None
    details: Optional[str] = None