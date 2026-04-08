from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelPhaseCommandIntent:
    orchestrator_id: str
    phase_name: str
    command: str
    intent_time: Optional[str] = None