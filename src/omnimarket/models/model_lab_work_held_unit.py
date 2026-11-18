"""A unit one lab work worker holds."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ModelLabWorkHeldUnit:
    """A unit one worker holds, with the clock it has been held on."""

    unit_id: str
    started: float
    limit_seconds: float
    last_logged: float
    past_limit_logged: bool = False
