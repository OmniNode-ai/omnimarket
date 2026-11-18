# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pricing block of the ONEX home's config.yaml, as a baseline overlay.

OMN-24615 AC2: a local run has no overlay in hand, so without this the
configured ``pricing.baseline_model`` was never read and every local saving was
priced against the code default. Only the ``pricing`` block is returned. A
missing, unreadable or malformed file is no configuration: the resolver then
falls through to its next source, exactly as with an empty overlay.
"""

from __future__ import annotations

from pathlib import Path

import yaml

__all__ = ["local_pricing_overlay"]


def local_pricing_overlay(onex_home: Path | None = None) -> dict[str, object]:
    """``{"pricing": {...}}`` from ``<onex_home>/config.yaml``, else ``{}``."""
    path = (onex_home or Path.home() / ".onex") / "config.yaml"
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}
    pricing = raw.get("pricing") if isinstance(raw, dict) else None
    return {"pricing": pricing} if isinstance(pricing, dict) else {}
