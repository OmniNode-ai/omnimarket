from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelPipelineStartCommand:
    pipeline_id: str
    ticket_id: str
    start_time: Optional[str] = None