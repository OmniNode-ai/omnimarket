from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelPipelineStartCommand:
    pipeline_id: str
    phase: str
    started_by: Optional[str] = None
    metadata: Optional[dict] = None