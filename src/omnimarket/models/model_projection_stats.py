# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ProjectionStats: in-memory counters of one projection runner (OMN-21536).

Moved out of ``projection/runner.py``, a baselined file that only shrinks, when the typed
projection request shim was added to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ProjectionStats:
    """In-memory projection stats."""

    events_projected: int = 0
    errors_count: int = 0
    last_projected_at: datetime | None = None
    topic_stats: dict[str, dict[str, int]] = field(default_factory=dict)
