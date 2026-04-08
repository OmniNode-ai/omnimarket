from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelPipelineCompletedEvent:
    pipeline_id: str
    status: str
    completed_at: Optional[str] = None
    error_message: Optional[str] = None