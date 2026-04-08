from dataclasses import dataclass
from typing import Optional


class PhaseCommandIntent:
    def __init__(self, build_id: str, phase: str):
        self.build_id = build_id
        self.phase = phase

    def __repr__(self):
        return f"PhaseCommandIntent(build_id={self.build_id}, phase={self.phase})"
