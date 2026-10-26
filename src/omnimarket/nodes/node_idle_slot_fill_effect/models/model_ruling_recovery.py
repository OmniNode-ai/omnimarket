# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recovery scan's lab-fill items and findings for this throughput run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelRulingRecoveryResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    found: list[tuple[str, str, str, str]] = field(default_factory=list)
