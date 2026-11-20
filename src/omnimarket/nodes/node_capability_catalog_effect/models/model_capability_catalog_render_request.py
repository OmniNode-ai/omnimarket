# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the capability-catalog render handler"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .model_capability_catalog import (
    Catalog,
)


@dataclass(frozen=True, slots=True)
class ModelCapabilityCatalogRenderRequest:
    """A catalog, the static facts it is rendered with, and the optional extra target.

    ``target`` is ``None`` for the three standing views, or ``"claude-md"`` to add the
    CLAUDE.md block as a fourth.
    """

    catalog: Catalog
    static_facts: Mapping[str, Any]
    target: str | None = None
