# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local receipt observation for a Codex provider refusal."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ModelCodexLaneLimitRequest:
    receipt_path: str
    placement_directory: str
    now: datetime | None = None
    completed_attempt: bool = False


@dataclass(frozen=True)
class ModelCodexLaneLimitResult:
    host: str | None = None
    limited_until: str | None = None
    observed_429: bool = False
