from dataclasses import dataclass

@dataclass
class OrchestratorResult:
    success: bool
    message: str