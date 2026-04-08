from dataclasses import dataclass

@dataclass
class PhaseCommandIntent:
    phase_name: str
    command: str