# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the capability-catalog lab collect handler"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .model_capability_catalog_sources import (
    Sources,
)


@dataclass(frozen=True, slots=True)
class ModelCapabilityCatalogCollectRequest:
    """The configured lab sources, the registry root and the collection timestamp."""

    sources: Sources
    registry_root: Path
    now: str
