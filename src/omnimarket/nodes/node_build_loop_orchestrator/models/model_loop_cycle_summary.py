from dataclasses import dataclass
from typing import Optional


class LoopCycleSummary:
    def __init__(self, build_id: str, phase: str, cycle_count: int):
        self.build_id = build_id
        self.phase = phase
        self.cycle_count = cycle_count

    def __repr__(self):
        return f"LoopCycleSummary(build_id={self.build_id}, phase={self.phase}, cycle_count={self.cycle_count})"
