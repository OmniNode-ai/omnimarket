"""Inputs to the adoption recovery effect."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ModelAdoptionRecoveryRequest:
    now: datetime
    env: Mapping[str, str]
    session_state_dir: Path
