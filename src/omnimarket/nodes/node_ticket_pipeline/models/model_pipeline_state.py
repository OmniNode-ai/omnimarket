from dataclasses import dataclass
from typing import Optional

@dataclass
class PipelineState:
    pipeline_id: str
    current_phase: Optional[str] = None
    status: str = 'pending'