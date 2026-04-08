from dataclasses import dataclass
from typing import Optional


class OrchestratorResult:
    def __init__(self, build_id: str, phase: str, success: bool):
        self.build_id = build_id
        self.phase = phase
        self.success = success

    def __repr__(self):
        return f"OrchestratorResult(build_id={self.build_id}, phase={self.phase}, success={self.success})"
