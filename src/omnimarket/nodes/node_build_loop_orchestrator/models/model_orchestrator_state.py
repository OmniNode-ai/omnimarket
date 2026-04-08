from dataclasses import dataclass
from typing import Optional


class OrchestratorState:
    def __init__(self, build_id: str, phase: str, status: str):
        self.build_id = build_id
        self.phase = phase
        self.status = status

    def __repr__(self):
        return f"OrchestratorState(build_id={self.build_id}, phase={self.phase}, status={self.status})"
