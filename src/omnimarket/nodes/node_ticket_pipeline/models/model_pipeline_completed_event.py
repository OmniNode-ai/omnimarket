from dataclasses import dataclass
from typing import Optional

@dataclass
class PipelineCompletedEvent:
    pipeline_id: str
    status: str
    completed_at: Optional[str] = None