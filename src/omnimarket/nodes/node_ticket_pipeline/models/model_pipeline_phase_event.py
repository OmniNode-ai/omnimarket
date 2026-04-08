from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelPipelinePhaseEvent:
    pipeline_id: str
    phase: str
    event_type: str
    timestamp: Optional[str] = None
    details: Optional[dict] = None