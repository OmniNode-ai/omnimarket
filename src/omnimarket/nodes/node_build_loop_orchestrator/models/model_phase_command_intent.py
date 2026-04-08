from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelPhaseCommandIntent:
    orchestrator_id: str
    phase: str
    command: str
    intent_metadata: Optional[dict] = None