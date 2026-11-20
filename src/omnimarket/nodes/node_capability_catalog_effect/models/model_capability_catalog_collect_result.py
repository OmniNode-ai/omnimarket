# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the capability-catalog lab collect handler"""

from __future__ import annotations

from dataclasses import dataclass

from .model_capability_catalog import (
    Catalog,
)


@dataclass(frozen=True, slots=True)
class ModelCapabilityCatalogCollectResult:
    """The catalog a lab collection produced: hosts, lanes and model servers."""

    catalog: Catalog
