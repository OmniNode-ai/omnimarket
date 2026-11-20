# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the capability-catalog render handler"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelCapabilityCatalogRenderResult:
    """Every rendered view, keyed by the file name it is written to."""

    views: Mapping[str, str]
