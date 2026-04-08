from dataclasses import dataclass

@dataclass
class PipelineStartCommand:
    pipeline_id: str
    phase: str