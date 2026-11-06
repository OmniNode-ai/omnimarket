# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The detached parent's placement read as one typed outcome (OMN-19998).

A script reads ``outcome`` and ``error`` instead of the exit code: ``placing`` (the wait budget ran out while the lane
was still being placed, exit 3) is not an error.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

PlacementOutcome = Literal["placed", "placing", "refused", "crashed"]

RESULT_PREFIX = "PLACEMENT_RESULT "


@dataclass(frozen=True)
class ModelPlacementWaitRequest:
    """Receipt read and runtime ports supplied by the detached host caller."""

    receipt: Path
    max_s: float
    lane: str
    alive: Callable[[], bool]
    poll_s: float = 1.0
    echo: Callable[[str], None] = print
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class ModelPlacementWaitResult:
    #: placed: the lane is running or done; placing: still being placed when the wait ended;
    #: refused: no host admitted it; crashed: the runner or the lane failed.
    outcome: PlacementOutcome
    #: True only for refused and crashed.
    error: bool
    exit_code: int
    lane: str
    #: The receipt's own status, or "dead" when the runner exited before placement.
    status: str
    host: str
    receipt: str
    reason: str = ""
    #: For placing: the command that reads the receipt to its end.
    wait_command: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    def line(self) -> str:
        return f"{RESULT_PREFIX}{self.to_json()}"
